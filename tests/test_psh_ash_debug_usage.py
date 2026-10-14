"""ASH已有debug程序时的参数错误不能触发破坏嵌套会话的Ctrl-C恢复。"""

import pytest
from camera_logs.collection.psh_dialogue import PshDialogue, PshSwitchError


@pytest.mark.parametrize('ls_response,success', [
    (b'ls\r\nbin etc proc usr\r\n# ', True),
    (b"ls\r\n'ls' Not Supported, Try 'help'\r\n# ", False),
])
async def test_ash_debug_usage_requires_positive_shell_probe(ls_response, success):
    dialogue = PshDialogue({'protocol': 'SSH', 'sshTarget': 'SLAVE_1'}, None)
    dialogue.feed(b'BusyBox built-in shell (ash)\n# ')
    sent = []

    async def write(data):
        sent.append(data)
        if data == b'debug\n':
            dialogue.feed(b"debug\r\nERROR: No parameters, see 'debug -h'\r\n# ")
        elif data == b'ls\n':
            dialogue.feed(ls_response)

    if success:
        await dialogue.ensure_ash(write, '\n', .1, verify_current_mode=True)
        assert dialogue.mode == 'ASH'
        assert sent == [b'debug\n', b'ls\n']
    else:
        with pytest.raises(PshSwitchError):
            await dialogue.ensure_ash(write, '\n', .1, verify_current_mode=True)
