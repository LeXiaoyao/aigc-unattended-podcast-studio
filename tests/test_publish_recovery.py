import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, AsyncMock

import xiaoyuzhou_auth_refresh as auth
from webapp import auto_publish as auto


class PublishRecoveryTests(unittest.TestCase):
    def test_cached_auth_still_checks_expiry(self):
        with patch.object(auth, 'warn_if_session_expiring') as warn, patch.object(auth, 'read_cached_auth', return_value={'accessToken': 'test'}), patch.object(auth, 'cache_is_usable', return_value=True):
            auth.ensure_auth(min_ttl_seconds=3600, force_refresh=False, notify_on_failure=True)
            warn.assert_called_once_with(notify_on_warn=True)

    def test_missing_cover_does_not_consume_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'record.json').write_text('{}')
            state = {'publish_attempts': 1}
            with patch.object(auto, '_notify_once'):
                asyncio.run(auto._auto_publish_episode(folder, state))
            self.assertEqual(state['publish_attempts'], 1)

    def test_cover_arrival_resumes_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'record.json').write_text('{}')
            state = {}
            with patch.object(auto, '_notify_once'), patch('webapp.publish_service.claim_episode_for_publish') as claim, patch('webapp.publish_service.run_publish_job', new_callable=AsyncMock) as publish:
                asyncio.run(auto._auto_publish_episode(folder, state))
                publish.assert_not_called()
                (folder / 'cover.jpeg').write_bytes(b'cover fixture')
                asyncio.run(auto._auto_publish_episode(folder, state))
                claim.assert_called_once()
                publish.assert_awaited_once()
                self.assertEqual(state['publish_attempts'], 1)

    def test_dry_run_notification_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(auto, '_dry_run', return_value=True):
            state = {}
            auto._notify_once(Path(tmp), state, 'sent', 'test', [])
            self.assertEqual(state, {})
            self.assertFalse((Path(tmp) / auto.STATE_FILENAME).exists())

    def test_backlog_only_notifies_unsubmitted_login_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, reason, submitted in [('20261017E1', '登录态失效', False), ('20261017E2', '封面缺失', False), ('20261016E1', '登录态失效', True), ('20261001E1', '登录态失效', False)]:
                folder = root / 'episodes' / name
                folder.mkdir(parents=True)
                (folder / 'record.json').write_text(json.dumps({'发布失败原因': reason, '发布失败时间': '2026-10-17T07:00:00'}))
                if submitted:
                    (folder / 'xiaoyuzhou_publish_result.json').write_text('{}')
            with patch.object(auto, 'ROOT_DIR', root), patch.object(auto, '_login_recovered_at', return_value=('2026-10-18T07:20:00', datetime(2026, 10, 18, 7, 20))), patch.object(auto, '_flow_config', return_value={'auto_publish': True}), patch('webapp.publish_service.current_episode_status', return_value='pending_confirmation'), patch('webapp.publish_service.episode_flow_type', return_value='solo'), patch.object(auto, '_notify_once') as notify, patch.object(auto, '_auto_publish_episode', new_callable=AsyncMock) as publish:
                asyncio.run(auto._notify_recovered_backlog(datetime(2026, 10, 18, 14)))
                self.assertEqual(notify.call_count, 1)
                self.assertEqual(notify.call_args.args[0].name, '20261017E1')
                publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
