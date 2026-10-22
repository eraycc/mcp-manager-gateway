"""Single-use browser-bound OAuth flows and bounded diagnostic tombstones."""
import hashlib
import time
from collections import OrderedDict

from fastapi import HTTPException

STATE_TTL = 600
MAX_RECENT_STATES = 4096


def state_error(reason, status=403):
    messages = {
        "state_missing": "缺少授权 state，请重新发起授权。",
        "state_expired": "授权 state 已过期，请关闭旧授权标签页后重新授权。",
        "state_used": "授权 state 已使用，请勿重复提交旧回调。",
        "state_mismatch": "授权 state 无效或已被新的流程替代，请关闭旧授权标签页后重新授权。",
        "code_exchange_failed": "授权码交换失败，请检查服务配置后重新授权。",
    }
    error = HTTPException(status, "OAuth " + reason + ": " + messages[reason])
    error.oauth_reason = reason
    return error


class OAuthFlows:
    def __init__(self):
        self.pending = {}
        self.recent = OrderedDict()
        self.generations = {}
        self.registration_epochs = {}

    @staticmethod
    def digest(state):
        return hashlib.sha256(state.encode()).hexdigest()

    def remember(self, state, pending, reason):
        self.recent[self.digest(state)] = {
            key: pending[key] for key in ("server_id", "user_id", "session_id") if key in pending
        } | {"reason": reason, "retain_until": time.time() + STATE_TTL}
        while len(self.recent) > MAX_RECENT_STATES:
            self.recent.popitem(last=False)

    def prune(self):
        now = time.time()
        for state, pending in list(self.pending.items()):
            if pending["expires"] <= now:
                self.pending.pop(state)
                self.remember(state, pending, "state_expired")
        for digest, record in list(self.recent.items()):
            if record["retain_until"] <= now:
                self.recent.pop(digest)

    def context(self, state):
        return self.pending.get(state) or self.recent.get(self.digest(state), {})

    def invalidate(self, server_id, owner=None, *, new_attempt=False):
        keys = {key for key in self.generations if key[0] == server_id and (owner is None or key[1] == owner)}
        if owner is not None:
            keys.add((server_id, owner))
        for key in keys:
            self.generations[key] = self.generations.get(key, 0) + 1
            if not new_attempt:
                self.registration_epochs[key] = self.registration_epochs.get(key, 0) + 1
        cleared = 0
        for state, pending in list(self.pending.items()):
            if pending["server_id"] == server_id and (owner is None or pending.get("owner", pending["user_id"]) == owner):
                self.pending.pop(state)
                self.remember(state, pending, "state_mismatch")
                cleared += 1
        self.prune()
        return cleared

    def begin(self, server_id, owner):
        self.prune()
        self.invalidate(server_id, owner, new_attempt=True)
        return self.generations[(server_id, owner)]

    def check(self, server_id, owner, generation):
        if self.generations.get((server_id, owner), 0) != generation:
            raise state_error("state_mismatch", 409)

    def consume(self, state, user_id, session_id, path):
        self.prune()
        if not state:
            raise state_error("state_missing")
        pending = self.pending.get(state)
        if pending is None:
            record = self.recent.get(self.digest(state), {})
            same_browser = record.get("user_id") == user_id and record.get("session_id") == session_id
            raise state_error(record["reason"] if same_browser else "state_mismatch")
        if pending["user_id"] != user_id or pending["session_id"] != session_id:
            raise state_error("state_mismatch")
        self.pending.pop(state)
        self.remember(state, pending, "state_used")
        if pending.get("callback_path", path) != path:
            raise state_error("state_mismatch")
        self.check(pending["server_id"], pending["owner"], pending["flow_generation"])
        return pending
