import os
from pathlib import Path
from cryptography.fernet import Fernet
from config import Config

KEY_FILE = Path.home() / ".betbot_fernet.key"


class SecurityManager:
    def __init__(self):
        self.key = self._load_or_create_key()
        self.cipher = Fernet(self.key)

    def _load_or_create_key(self) -> bytes:
        env = (Config.ENCRYPTION_KEY or "").strip()
        if env and env != "auto":
            # Accept raw Fernet key string
            try:
                if isinstance(env, str):
                    return env.encode() if not env.startswith("gAAAA") else env.encode()
            except Exception:
                pass
        if KEY_FILE.exists():
            return KEY_FILE.read_bytes().strip()
        key = Fernet.generate_key()
        KEY_FILE.write_bytes(key)
        try:
            os.chmod(KEY_FILE, 0o600)
        except Exception:
            pass
        return key

    def encrypt_file(self, filepath: str):
        path = Path(filepath)
        if not path.exists():
            return
        data = path.read_bytes()
        path.with_suffix(path.suffix + ".enc").write_bytes(self.cipher.encrypt(data))
        self.secure_delete(str(path))

    def decrypt_file(self, filepath: str):
        enc = Path(filepath + ".enc")
        if not enc.exists():
            return None
        return self.cipher.decrypt(enc.read_bytes())

    def secure_delete(self, filepath: str):
        path = Path(filepath)
        if not path.exists():
            return
        try:
            size = path.stat().st_size
            with open(path, "ba+") as f:
                f.seek(0)
                f.write(os.urandom(size))
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
        try:
            path.unlink()
        except Exception:
            pass
