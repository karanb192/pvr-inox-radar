import json
import os

import pvr_client


def claim_invitation():
    try:
        directory = pvr_client.cache_dir()
        lock = os.path.join(directory, "star-invitation.lock")
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        try:
            with open(os.path.join(directory, pvr_client.STATE_FILE)) as fh:
                state = json.load(fh)
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict) or state.get("star_invitation_shown"):
            return False
        state["star_invitation_shown"] = True
        pvr_client.save_cache(pvr_client.STATE_FILE, state)
        return True
    except (OSError, ValueError):
        return False
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


if __name__ == "__main__":
    print("offer" if claim_invitation() else "skip")
