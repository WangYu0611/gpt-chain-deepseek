import hashlib
import json
from pathlib import Path
import uuid

root = Path(__file__).resolve().parent.parent
old = json.loads((root / '.private/live-cycle.json').read_text(encoding='utf-8-sig'))
config = {'enabled': True, 'experiment': 'GPTDSH_SERIAL_M0', 'nonce': str(uuid.uuid4()),
          'workflow_sha': 'a908bf3957edb91bd842992cf83cab88b3d3b2ef', 'expires_at': '2026-10-28T08:58:46Z',
          'arm_comment_id': None, 'arm_body_sha256': None, 'dsh': old['dsh']}
config['dsh']['usage_authorization'] = {'source': 'explicit_user_request', 'scope': 'single_issue_smoke', 'user_text': '用户允许本次90分钟受限实测，并澄清为两个简单文档任务连续执行，第三任务只发布。'}
arm = {k: config[k] for k in ('experiment', 'nonce', 'workflow_sha', 'expires_at')}
arm.update(authorized=True, max_model_requests=2, control_issue=5, tasks=['TASK_M0_1','TASK_M0_2'], publish_only='TASK_M0_3')
body = '<!-- serial-arm:v1 -->\n' + json.dumps(arm, ensure_ascii=False, separators=(',', ':'))
config['arm_body_sha256'] = hashlib.sha256(body.encode('utf-8')).hexdigest()
(root / '.private/serial-smoke.json').write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
(root / '.runtime/serial-arm.txt').write_text(body, encoding='utf-8')
print(body)
