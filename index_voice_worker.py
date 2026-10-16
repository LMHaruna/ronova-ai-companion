"""IndexTTS uses the same job queue, cancellation and fragments as GPT-SoVITS."""
import voice_worker
from index_voice_backend import initialize

voice_worker.initialize = initialize
voice_worker.SYNTHESIZE_ON_MAIN_THREAD = True

if __name__ == '__main__':
    voice_worker.main()
