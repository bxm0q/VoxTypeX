import sys
from array import array

from .interfaces import MIN_RECORDING_SECONDS, AudioClip, OperationContext


class PCM16Processor:
    def process(self, audio: AudioClip, context: OperationContext) -> AudioClip | None:
        context.checkpoint()
        if audio.channels != 1 or audio.sample_width != 2 or audio.sample_rate != 16000 or len(audio.pcm) % 2:
            raise ValueError("STT requires mono PCM16 at 16000 Hz")
        if audio.duration < MIN_RECORDING_SECONDS and audio.speech_detected is not True:
            return None
        samples = array("h", audio.pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        energy = 0
        for start in range(0, len(samples), 4096):
            context.checkpoint()
            energy += sum(sample * sample for sample in samples[start : start + 4096])
        return audio if energy / len(samples) >= (32768 * 0.0001) ** 2 else None
