import warnings
from dataclasses import dataclass
import numpy as np
from mms_msg.sampling.utils import collate_fn
import padertorch as pt


def _get_valid_overlap_region(examples, max_concurrent_spk, current_source, use_vad=False):
    """
    Compute maximum overlap that guarantees that no more than  max_concurrent_spk are active at the same time.
    Note: This function underestimates the maximal overlap to ensure regions sampled as silence or
    repetitions of the same speaker will have no overlapping speech added later in the sampling   .
    Args:
        examples:
        max_concurrent_spk:
        use_vad: (optional) When set to True the keys that represent the alignment of the vad data are
            used to compute the valid overlap region.
    Returns:

    """
    if use_vad:
        speaker_end = np.asarray(examples['offset']['aligned_source']) + np.asarray(
            examples['num_samples']['aligned_source'])
    else:
        speaker_end = np.asarray(examples['offset']['original_source']) + np.asarray(
            examples['num_samples']['observation'])
    speaker_id = examples['speaker_id']

    return get_allowed_max_overlap(speaker_end, speaker_id, max_concurrent_spk, current_source['speaker_id'])


def get_allowed_max_overlap(speaker_end, speaker_ids, max_concurrent_spk, current_speaker_id):
    """
    >>> get_allowed_max_overlap([1], ['a'], max_concurrent_spk=2, current_speaker_id='a')
    """
    speaker_end = np.asarray(speaker_end)
    assert speaker_end.ndim == 1, speaker_end.shape

    # Pad speaker_ids and speaker_end to have at least max_concurrent_spk entries
    speaker_ids = speaker_ids + [None] * (max_concurrent_spk - len(speaker_end))
    speaker_end = np.concatenate([
        speaker_end,
        np.zeros_like(speaker_end, shape=max(max_concurrent_spk - speaker_end.shape[-1], 0))
    ])

    # Only keep end points relevant for the current sampling
    speaker_idx = np.argsort(speaker_end)[-max_concurrent_spk:]
    speaker_end = np.array(speaker_end)[speaker_idx]
    speaker_ids = np.array(speaker_ids)[speaker_idx]

    if current_speaker_id in speaker_ids:
        # Find last appearance of the speaker
        spk_pos = list(speaker_ids)[::-1].index(current_speaker_id)
        max_concurrent_spk = spk_pos + 1  # remove index shift introduced by flipping the list
    max_overlap = speaker_end[-1] - speaker_end[-max_concurrent_spk]
    return max_overlap


@dataclass(frozen=True)
class OverlapSampler(pt.Configurable):
    max_concurrent_spk: int

    def __call__(self, examples, current_source, rng):
        examples = collate_fn(examples)
        maximum_overlap = _get_valid_overlap_region(examples, self.max_concurrent_spk, current_source)

        offset = self.sample_offset(examples, maximum_overlap, rng)

        return offset

    def sample_offset(self, examples, maximum_overlap, rng):
        return NotImplementedError


@dataclass(frozen=True)
class UniformOverlapSampler(OverlapSampler):
    """
    Attributes:
        max_concurrent_spk: The maximum number of concurrently active speakers
        p_silence: Approximate target value for silence probability after each
            utterance. The resulting true probability is likely higher than
            `p_silence` due to rejection sampling.
        maximum_silence: The maximum amount of silence allowed between two
            utterances, in samples
        maximum_overlap: The maximum amount of overlap allowed between two
            utterances, in samples
        minimum_silence: The minimum amount of silence between two utterances
            if silence is sampled, in samples
        minimum_overlap: The minimum amount of overlap between two utterances
            if overlap is sampled, in samples
        margin: The minimum distance between two utterances that would violate
            `max_concurrent_spk`. Can be used to account for the reverberation
            tail
    """
    p_silence: float
    maximum_silence: int
    maximum_overlap: int
    minimum_silence: int = 0
    soft_minimum_overlap: int = 0
    hard_minimum_overlap: int = 0
    margin: int = 0

    def __post_init__(self):
        assert self.minimum_silence >= self.margin, (self.minimum_silence, self.margin)
        assert self.minimum_silence < self.maximum_silence, (self.minimum_silence, self.maximum_silence)
        assert self.soft_minimum_overlap <= self.maximum_overlap, (self.soft_minimum_overlap, self.maximum_overlap)
        assert self.hard_minimum_overlap <= self.maximum_overlap, (self.hard_minimum_overlap, self.maximum_overlap)

    def sample_offset(self, examples, maximum_overlap, rng):
        # Sample the shift (relative to the latest speaker_end) with rejection
        # sampling so that never more than max_concurrent_spk are active at the
        # same time. This means that we see more silence than self.p_silence
        if maximum_overlap <= self.hard_minimum_overlap:
            # We can't sample overlap here, so sample silence
            shift = max(self._sample_silence(rng), self.margin)
        elif maximum_overlap <= self.soft_minimum_overlap:
            # Return maximum possible overlap in this case
            shift = -maximum_overlap + self.margin
        else:
            for _ in range(100):    # Arbitrary upper bound for num rejections
                shift = self._sample_shift(rng)
                if shift > -maximum_overlap + self.margin:
                    break
            else:
                # If the shift was rejected 100 times in a row, the region for
                # overlap is probably too small. Fall back to sampling silence.
                # This distorts the distribution slightly, but probably not
                # notably.
                warnings.warn(
                    'Offset sampling failed 100 times in a row, sampling '
                    'silence!'
                )
                shift = self._sample_silence(rng)

        # The final offset is global, but the shift is relative to the last
        # speaker end time, so shift
        offset = np.max(
            np.asarray(examples['offset']['original_source']) +
            np.asarray(examples['num_samples']['observation'])
        ) + shift
        return offset

    def _sample_shift(self, rng):
        if rng.uniform(0, 1) <= self.p_silence:
            shift = self._sample_silence(rng)
        else:
            shift = -self._sample_overlap(rng)
        return shift

    def _sample_silence(self, rng):
        silence = rng.integers(self.minimum_silence, self.maximum_silence)
        return silence

    def _sample_overlap(self, rng):
        overlap = rng.integers(self.hard_minimum_overlap, self.maximum_overlap)
        return overlap
