import io
import zlib
import struct

MATRIX_WIDTH = 32
MATRIX_HEIGHT = 26
FRAME_PIXEL_SIZE = MATRIX_WIDTH * MATRIX_HEIGHT * 3  # 2496 bytes

def parse_q4x_data(file_bytes: bytes):
    """
    Parses Q4X file binary content.
    Returns:
        dict: {
            "name": str,
            "duration_ms": int,
            "audio_bytes": bytes or None,
            "audio_ext": str or None,
            "frames": list of (pixel_bytes, frame_duration_ms)
        }
    """
    stream = io.BytesIO(file_bytes)
    
    # 1. Header
    magic = stream.read(4)
    if magic not in (b"Q4X1", b"Q4X2"):
        raise ValueError("Invalid file magic. Must be Q4X1 or Q4X2.")
    
    width = struct.unpack(">H", stream.read(2))[0]
    if width != MATRIX_WIDTH:
        raise ValueError(f"Invalid matrix width: {width}. Expected {MATRIX_WIDTH}.")
        
    height = struct.unpack(">H", stream.read(2))[0]
    if height != MATRIX_HEIGHT:
        raise ValueError(f"Invalid matrix height: {height}. Expected {MATRIX_HEIGHT}.")
        
    # 2. QP4 section (skip it)
    qp4_size = struct.unpack(">I", stream.read(4))[0]
    stream.seek(stream.tell() + qp4_size)
    
    # 3. QPR section
    qprz_size = struct.unpack(">I", stream.read(4))[0]
    qprz = stream.read(qprz_size)
    if len(qprz) != qprz_size:
        raise ValueError("Truncated QPR compressed data.")
        
    qpr_bytes = zlib.decompress(qprz)
    
    # 4. Optional audio
    audio_bytes = None
    audio_ext = None
    
    audio_size_bytes = stream.read(4)
    if len(audio_size_bytes) == 4:
        audio_size = struct.unpack(">I", audio_size_bytes)[0]
        if audio_size > 0:
            audio_bytes = stream.read(audio_size)
            if len(audio_bytes) != audio_size:
                raise ValueError("Truncated audio data.")
            # Check magic bytes for audio format
            sound_magic = audio_bytes[0:4]
            audio_ext = "ogg" if sound_magic == b"OggS" else "mp3"
            
    # 5. Parse QPR stream
    qpr_stream = io.BytesIO(qpr_bytes)
    
    file_version = qpr_stream.readline().decode("ascii").strip()
    if file_version != "qpr v1":
        raise ValueError(f"Invalid QPR version: {file_version}. Expected 'qpr v1'.")
        
    animation_name = qpr_stream.readline().decode("utf-8").strip()
    audio_flag = qpr_stream.readline().decode("ascii").strip()
    total_duration_ms = int(qpr_stream.readline().decode("ascii").strip())
    
    frames = []
    while True:
        pixel_data = qpr_stream.read(FRAME_PIXEL_SIZE)
        if not pixel_data:
            break
        if len(pixel_data) != FRAME_PIXEL_SIZE:
            # Reached end or incomplete frame
            break
            
        duration_bytes = qpr_stream.read(4)
        if len(duration_bytes) < 4:
            frame_duration_ms = 20  # default
        else:
            frame_duration_ms = struct.unpack(">I", duration_bytes)[0]
            
        frames.append((pixel_data, frame_duration_ms))
        
    return {
        "name": animation_name,
        "duration_ms": total_duration_ms,
        "audio_bytes": audio_bytes,
        "audio_ext": audio_ext,
        "frames": frames
    }
