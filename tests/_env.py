"""Shared test bootstrap: point every data file at a fresh temp dir BEFORE the
app modules are imported (they read paths from the environment at import)."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATA = tempfile.mkdtemp(prefix='cruxwire-test-')
for var, name in [('STATE_FILE', 'state.json'), ('DIGEST_FILE', 'digest.json'),
                  ('FEEDS_FILE', 'feeds.json'), ('SETTINGS_FILE', 'settings.json'),
                  ('CATEGORIES_FILE', 'categories.json'), ('RUNS_FILE', 'runs.json'),
                  ('EMBEDDINGS_FILE', 'embeddings.json'), ('PREFERENCES_FILE', 'preferences.json'),
                  ('IMAGE_CACHE_FILE', 'image_cache.json'), ('EMBED_CACHE_FILE', 'embed_cache.json'),
                  ('ONBOARDING_POOL_FILE', 'onboarding_pool.json')]:
    os.environ[var] = os.path.join(DATA, name)
os.environ['STATIC_DIR'] = ROOT
os.environ.setdefault('OLLAMA_HOST', 'http://127.0.0.1:9')   # nothing listens: Ollama "down"


def reset():
    """Remove every data file so each test starts clean."""
    import images
    for f in os.listdir(DATA):
        try:
            os.unlink(os.path.join(DATA, f))
        except OSError:
            pass
    images._cache = None
