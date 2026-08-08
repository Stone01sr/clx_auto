"""荼蘼界面操作的全局互斥锁。

荼蘼是靠"点固定坐标 + 截屏认图"来操作的，这类操作对界面状态有很强的假设：
展开方案下拉列表 -> 在列表里认出要跑的方案 -> 点中它，中间只要被别人插一脚
（监控线程点掉异常退出的错误弹窗、另一处把窗口置前、另一处截图去扫描空闲行），
下拉列表就会被收起来或者被挡住，紧接着的认图必然失败——而在队列模式下
这一失败会让整个角色重新登录一遍，代价很大。

所以凡是"要连续操作荼蘼界面"的代码段，都先拿这把锁，把整段变成原子的。
锁用RLock：同一线程里嵌套加锁（比如整段注册脚本已经持锁，里面再调用置前/点弹窗
这些自己也想加锁的小步骤）不会把自己锁死。
"""
import logging
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_lock = threading.RLock()

# 等锁超过这个秒数才打日志，避免正常情况下（几乎不用等）刷屏
_WAIT_LOG_THRESHOLD_SEC = 1.0


@contextmanager
def tu_mi_ui(operation, timeout=None):
    """获取荼蘼界面锁，yield出是否拿到锁。

    timeout=None（默认）：一直等到拿到为止，yield True——主流程（登录/注册脚本）用这种，
    这些操作必须做完，不能因为等锁就跳过。
    timeout=N：最多等N秒，等不到就yield False，由调用方决定跳过——监控线程用这种，
    它只是周期性地看一眼状态，等不到锁就跳过本轮，下一轮再来，绝不能把自己卡死。
    """
    started = time.time()
    acquired = _lock.acquire() if timeout is None else _lock.acquire(timeout=timeout)
    waited = time.time() - started
    if not acquired:
        logger.warning("等待荼蘼界面锁超时（已等%.1f秒），跳过本次「%s」", waited, operation)
        yield False
        return
    if waited >= _WAIT_LOG_THRESHOLD_SEC:
        logger.info("「%s」等待荼蘼界面锁%.1f秒后开始执行", operation, waited)
    try:
        yield True
    finally:
        _lock.release()
