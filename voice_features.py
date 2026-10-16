"""Reuse the speaker embedding of one unchanged reference waveform."""


class ReferenceSpeakerCache:
    def __init__(self, compute):
        self.compute = compute
        self.source = None
        self.version = None
        self.embedding = None
        self.hits = 0
        self.misses = 0

    def __call__(self, waveform):
        # A strong reference prevents reuse of an object's identity. PyTorch's
        # version counter also invalidates an in-place edit of that waveform.
        try:
            version = waveform._version
        except (AttributeError, RuntimeError):
            version = None
        if version is not None and self.source is waveform and self.version == version:
            self.hits += 1
            return self.embedding
        self.misses += 1
        embedding = self.compute(waveform)
        # Unversioned/inference tensors cannot safely detect in-place changes.
        self.source = waveform if version is not None else None
        self.version = version
        self.embedding = embedding if version is not None else None
        return embedding
