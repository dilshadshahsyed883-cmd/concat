"""NVDEC, CUDA surfaces and NVENC for the hybrid renderer."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess

from .contract import FrameRate, cuda, rgba


def _sdk():
    try:
        import PyNvVideoCodec as nvc
    except (ImportError, OSError, RuntimeError) as error:
        raise RuntimeError("NVDEC/NVENC requires PyNvVideoCodec and the NVIDIA driver") from error
    return nvc


def _check_resolution(width: int, height: int):
    if any(type(n) is not int or n < 2 or n % 2 for n in (width, height)):
        raise ValueError("4:2:0 video requires positive even dimensions")


def _context(cp):
    # Initialize CuPy's primary context before passing it to PyNvVideoCodec.
    cp.cuda.runtime.free(0)
    context = cp.cuda.driver.ctxGetCurrent()
    if not context:
        raise RuntimeError("No active CUDA context")
    return context


def _check_sdr_input(path: Path):
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=pix_fmt,color_transfer", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        streams = json.loads(probe.stdout)["streams"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise ValueError("Unable to identify the video input") from error
    if len(streams) != 1:
        raise ValueError("NVDEC SDR input must have exactly one video stream")
    stream = streams[0]
    if (stream.get("pix_fmt") not in {"yuv420p", "yuvj420p"}
            or stream.get("color_transfer") in {"smpte2084", "arib-std-b67"}):
        raise ValueError("NVDEC SDR input requires 8-bit 4:2:0 SDR video")


@dataclass(frozen=True)
class DecodedFrame:
    index: int
    source_pts: int
    pixels: object


class Nvdec:
    """Decode a seekable video on NVDEC to CUDA SDR RGBA surfaces."""

    def __init__(self, path: str | Path):
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError("NVDEC input must be a local seekable video file")
        _check_sdr_input(path)
        cp = cuda()
        self._gpu = cp.cuda.Device().id
        nvc = _sdk()
        self._decoder = nvc.SimpleDecoder(
            path, gpu_id=self._gpu, cuda_context=_context(cp),
            use_device_memory=True, output_color_type=nvc.OutputColorType.RGBP,
        )

    def __len__(self):
        return len(self._decoder)

    def decode(self, frame_index: int) -> DecodedFrame:
        if type(frame_index) is not int or not 0 <= frame_index < len(self):
            raise IndexError("Frame index outside the decoded stream")
        cp = cuda()
        if cp.cuda.Device().id != self._gpu:
            raise ValueError("NVDEC session belongs to a different CUDA device")
        decoded = self._decoder[frame_index]
        if decoded.__dlpack_device__() != (2, self._gpu):
            raise RuntimeError("NVDEC did not return a CUDA DLPack surface")
        rgb = cp.from_dlpack(decoded)
        if rgb.ndim != 3 or rgb.shape[0] != 3 or rgb.dtype != cp.uint8:
            raise ValueError("NVDEC SDR path requires an 8-bit planar RGB surface")
        height, width = rgb.shape[1:]
        pixels = cp.empty((height, width, 4), dtype=cp.float32)
        pixels[..., :3] = rgb.transpose(1, 2, 0) * cp.float32(1 / 255)
        pixels[..., 3] = 1
        # Complete the conversion before the decoder can recycle its surface.
        cp.cuda.get_current_stream().synchronize()
        return DecodedFrame(frame_index, decoded.getPTS(), pixels)

    def frame(self, frame_index: int):
        return self.decode(frame_index).pixels


def nv12_from_rgba(pixels):
    """Pack BT.709 transfer-encoded SDR RGB as limited-range NV12 on CUDA.

    Transparent input is composited over black before encoding.
    """
    pixels = rgba(pixels)
    cp = cuda()
    height, width = pixels.shape[:2]
    _check_resolution(width, height)
    rgb = cp.clip(pixels[..., :3], 0, 1) * cp.clip(pixels[..., 3:4], 0, 1)
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    chroma_b = ((blue - luma) / 1.8556).reshape(height // 2, 2, width // 2, 2)
    chroma_r = ((red - luma) / 1.5748).reshape(height // 2, 2, width // 2, 2)
    packed = cp.empty((height * 3 // 2, width), dtype=cp.uint8)
    packed[:height] = cp.rint(16 + 219 * luma)
    packed[height:, 0::2] = cp.rint(128 + 224 * chroma_b.mean(axis=(1, 3)))
    packed[height:, 1::2] = cp.rint(128 + 224 * chroma_r.mean(axis=(1, 3)))
    return packed


@dataclass(frozen=True)
class EncodedPacket:
    data: bytes
    frame_index: int
    picture_type: int


class _Planes:
    """Expose the two views NVENC needs without transferring pixel data."""

    def __init__(self, packed, width: int, height: int):
        self.luma = packed[:height].reshape(height, width, 1)
        self.chroma = packed[height:].reshape(height // 2, width // 2, 2)

    def cuda(self):
        return [self.luma, self.chroma]


def _packets(result):
    return tuple(EncodedPacket(bytes(packet["data"]), packet["timestamp"],
                               packet["picture_type"]) for packet in result)


class Nvenc:
    """CFR GPU encoder for packed NV12 or P010 video surfaces."""

    def __init__(self, width: int, height: int, rate: FrameRate, *,
                 codec: str = "h264", pixel_format: str = "NV12",
                 bitrate: int = 6_000_000):
        _check_resolution(width, height)
        if codec not in ("h264", "hevc") or pixel_format not in ("NV12", "P010"):
            raise ValueError("Supported NVENC outputs are H.264/NV12, HEVC/NV12 and HEVC/P010")
        if pixel_format == "P010" and codec != "hevc":
            raise ValueError("P010 encoding requires HEVC")
        fps = rate.seconds(1)
        if fps.numerator != 1:
            raise ValueError("NVENC currently requires a whole-number frame rate")
        if type(bitrate) is not int or not 0 < bitrate <= 2**32 - 1:
            raise ValueError("NVENC bitrate must be a positive 32-bit integer")

        self.width, self.height, self.rate = width, height, rate
        self.codec, self.pixel_format = codec, pixel_format
        cp = cuda()
        self._nvc = _sdk()
        self._gpu = cp.cuda.Device().id
        self._stream = cp.cuda.Stream(non_blocking=True)
        self._encoder = self._nvc.CreateEncoder(
            width, height, pixel_format, False, gpu_id=self._gpu,
            cudacontext=_context(cp), cudastream=self._stream.ptr,
            codec=codec, fps=fps.denominator, bitrate=bitrate,
            # Raw Annex B muxers assign CFR timestamps in packet order.
            # Avoid reordering frames that have no container timestamps yet.
            preset="P4", bf=0, repeatspspps=1,
        )
        self._next_index = 0
        self._finished = False

    def sequence_parameters(self) -> bytes:
        return bytes(self._encoder.GetSequenceParams())

    def encode(self, packed, frame_index: int) -> tuple[EncodedPacket, ...]:
        if self._finished:
            raise RuntimeError("NVENC session was already flushed")
        if type(frame_index) is not int or frame_index != self._next_index:
            raise ValueError("NVENC frames must arrive in contiguous timeline order from frame zero")
        cp = cuda()
        if not isinstance(packed, cp.ndarray):
            raise TypeError("NVENC requires a GPU-resident CuPy surface")
        if self._gpu != cp.cuda.Device().id or packed.device.id != self._gpu:
            raise ValueError("NVENC input is on a different CUDA device")
        expected_dtype = cp.uint8 if self.pixel_format == "NV12" else cp.uint16
        if (packed.shape != (self.height * 3 // 2, self.width)
                or packed.dtype != expected_dtype or not packed.flags.c_contiguous):
            raise ValueError(f"NVENC requires a contiguous packed {self.pixel_format} surface")
        # Synchronize the producer before the native encoder consumes its pointers.
        cp.cuda.get_current_stream().synchronize()
        params = self._nvc.NV_ENC_PIC_PARAMS()
        params.inputTimeStamp = frame_index
        result = self._encoder.Encode(_Planes(packed, self.width, self.height), params)
        # Keep the source allocation alive until the encoder stream has copied it.
        self._stream.synchronize()
        self._next_index += 1
        return _packets(result)

    def finish(self) -> tuple[EncodedPacket, ...]:
        if self._finished:
            return ()
        if cuda().cuda.Device().id != self._gpu:
            raise ValueError("NVENC session belongs to a different CUDA device")
        self._finished = True
        return _packets(self._encoder.EndEncode())
