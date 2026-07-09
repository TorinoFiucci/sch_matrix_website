import io
import math
import tempfile
import zlib

import gizeh
import pandas
from moviepy.editor import *
from tqdm import tqdm

# region Constants
fps = 50
matrix_width = 32
matrix_height = 26
qpr_frame_size = 32 * 26 * 3  # Not modifiable
window_size = 20
pixel_size = 10
video_width = 1920
video_height = 1080  # endregion


def parse_q4x(file_name, tmpdir):
    with open(file_name, "rb") as q4x:
        # region header
        file_magic = q4x.read(4)
        if not file_magic or (file_magic != b"Q4X1" and file_magic != b"Q4X2"):
            raise RuntimeError("Invalid file magic")

        width = int.from_bytes(q4x.read(2), byteorder="big", signed=False)
        if not width or width != matrix_width:
            raise RuntimeError("Invalid matrix width")

        height = int.from_bytes(q4x.read(2), byteorder="big", signed=False)
        if not height or height != matrix_height:
            raise RuntimeError("Invalid matrix height")
        # endregion

        # region QP4
        q4z_size = int.from_bytes(q4x.read(4), byteorder="big", signed=False)
        if not q4z_size or q4z_size <= 0:
            raise RuntimeError("Missing qp4")

        q4x.seek(q4x.tell() + q4z_size)
        # endregion

        # region QPR
        qprz_size = int.from_bytes(q4x.read(4), byteorder="big", signed=False)
        if not qprz_size or qprz_size <= 0:
            raise RuntimeError("Missing qpr")

        qprz = q4x.read(qprz_size)
        if not qprz:
            raise RuntimeError("Missing qpr")

        qpr = zlib.decompress(qprz)
        # endregion

        # region optional MP3/OGG
        sound_file_name = None
        sound_size = int.from_bytes(q4x.read(4), byteorder="big", signed=False)
        if sound_size != 0:
            sound = q4x.read(sound_size)
            if not sound:
                raise RuntimeError("Missing sound file")

            # TODO sound header check
            sound_magic = sound[0:4]
            sound_ext = "ogg" if sound_magic == b"OggS" else "mp3"

            sound_file_name = os.path.join(tmpdir, f"sound.{sound_ext}")
            with open(sound_file_name, "wb") as sf:
                sf.write(sound)
        # endregion

    return qpr, sound_file_name


def parse_qpr(qpr):
    header_length = 0

    # region header
    qpr_stream = io.BytesIO(qpr)
    file_version = qpr_stream.readline().decode("ascii")
    if not file_version or file_version != "qpr v1\n":
        raise RuntimeError("Invalid qpr")

    header_length += len(file_version)

    animation_name = qpr_stream.readline().decode("utf_8")
    if not animation_name:
        raise RuntimeError("Invalid qpr")

    header_length += len(animation_name)

    audio = qpr_stream.readline().decode("ascii")
    if not audio:
        raise RuntimeError("Invalid qpr")

    header_length += len(audio)

    animation_duration_ms = qpr_stream.readline().decode("ascii")
    if not animation_duration_ms:
        raise RuntimeError("Invalid qpr")

    animation_duration_ms = int(animation_duration_ms)
    if animation_duration_ms % 20 != 0:
        animation_duration_bad = animation_duration_ms
        animation_duration_ms = math.ceil(animation_duration_ms / 20) * 20

        print(f"Invalid animation duration {animation_duration_bad}, new value: {animation_duration_ms}")
    # endregion

    length_without_header = len(qpr) - header_length

    return animation_duration_ms, qpr_stream, length_without_header


def main():
    file_name = sys.argv[1]
    df = pandas.read_csv("window_coordinates.csv", delimiter=";")

    stage_x = df["0x"][0]
    stage_y = df["0y"][0]
    stage_width = df["15x"][12] - stage_x + window_size
    stage_height = df["15y"][12] - stage_y + window_size

    with tempfile.TemporaryDirectory(dir=".") as tmpdir:
        qpr, sound_file_name = parse_q4x(file_name, tmpdir)
        animation_duration_ms, qpr_stream, length_without_header = parse_qpr(qpr)

        video_duration = min(animation_duration_ms / 1000, 180)  # Max 3 minutes
        durations_s = []
        frames = []

        frame = qpr_stream.read(qpr_frame_size)
        with tqdm(desc="Creating frames from qpr", total=length_without_header) as pbar:
            surface = gizeh.Surface(width=stage_width, height=stage_height, bg_color=(0, 0, 0))
            while frame and sum(durations_s) <= 180:
                if len(frame) != qpr_frame_size:
                    raise RuntimeError("Invalid qpr frame")

                frame_iter = iter(frame)
                for row in range(matrix_height):
                    for col in range(matrix_width):
                        r = next(frame_iter) / 255
                        g = next(frame_iter) / 255
                        b = next(frame_iter) / 255

                        window_col_idx = col // 2
                        window_row_idx = row // 2
                        window_x = df[f"{window_col_idx}x"][window_row_idx] - stage_x
                        window_y = df[f"{window_col_idx}y"][window_row_idx] - stage_y

                        pixel = gizeh.square(l=pixel_size, xy=[(window_x + 5) + (col % 2 * pixel_size),
                                                               (window_y + 5) + (row % 2 * pixel_size)], fill=(r, g, b))
                        pixel.draw(surface)

                # surface.write_to_png(os.path.join(tmpdir, f"{len(frames)}.png"))
                frames.append(surface.get_npimage())

                frame_duration_ms = int.from_bytes(qpr_stream.read(4), byteorder="big", signed=False)
                if frame_duration_ms % 20 != 0:
                    frame_duration_ms_bad = frame_duration_ms
                    frame_duration_ms = math.ceil(frame_duration_ms / 20) * 20

                    print(f"Invalid frame duration {frame_duration_ms_bad}, new value: {frame_duration_ms}")

                durations_s.append(frame_duration_ms / 1000)
                pbar.update(qpr_frame_size + 4)
                frame = qpr_stream.read(qpr_frame_size)

        durations_sum_s = sum(durations_s)
        video_duration = max(video_duration, min(durations_sum_s, 180))

        print(f"Frame count: {len(frames)}")
        print(f"Frame duration sum: {durations_sum_s} s")
        print(f"Video duration will be: {video_duration} s, {video_duration / 60} m")

        main_clip = ColorClip(size=(video_width, video_height), color=[0, 0, 0], duration=video_duration)

        qpr_clip = ImageSequenceClip(sequence=frames, durations=durations_s)
        qpr_clip = qpr_clip.set_position((stage_x, stage_y))
        qpr_clip = qpr_clip.set_duration(main_clip.duration)

        if sound_file_name:
            qpr_clip_audio = AudioFileClip(sound_file_name)
            qpr_clip = qpr_clip.set_audio(qpr_clip_audio)

        video = CompositeVideoClip([main_clip, qpr_clip], use_bgclip=True)
        video.write_videofile(f"{file_name[:-4]}.mp4", fps=fps, codec="h264_nvenc", audio_fps=48000,
                              audio_codec="aac",
                              audio_bitrate="320K")
        if sound_file_name:
            qpr_clip_audio.close()


if __name__ == '__main__':
    main()
