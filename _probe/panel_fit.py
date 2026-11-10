# 设置面板文字裁切检查：面板宽度固定 340，行内 label 与右侧控件抢宽度，
# 谁的超长就会被静默裁掉（2026-11-10 截图发现：副标题被切、「秒显示」按钮变省略号）。
#
# 白盒判据：每个 QLabel 的实宽 < sizeHint 宽度 ⇒ 文字被裁（Qt 不 elide，直接切）。
# 跑法：py -3.10 -X utf8 -I _probe/panel_fit.py
import os
import shutil
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ["FLIPCLOCK_AUTOTEST"] = "1"

_SRC = Path(__file__).resolve().parent.parent / "src"
shutil.rmtree(_SRC / "__pycache__", ignore_errors=True)  # 防 stale pyc
sys.path.insert(0, str(_SRC))

from PySide6.QtWidgets import QApplication, QLabel, QToolButton, QWidget  # noqa: E402

import settings as settings_mod  # noqa: E402
from settings_panel import SettingsPanel  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


app = QApplication(sys.argv)
cfg = settings_mod.load()
snap = lambda: {"status": "init", "offset_s": None}  # noqa: E731
# 复刻真实几何：面板挂在主窗口（全屏 1280 宽）下，refit() 把宽度锁到 340
host = QWidget()
host.resize(1280, 800)
panel = SettingsPanel(cfg, snap, parent=host)
panel.refit()
host.show()  # 必须真显示：否则面板布局不激活，子控件保持自然宽度、不会暴露挤压
panel.show()
app.processEvents()
check("panel width locked to 340", panel.width() == 340, f"w={panel.width()}")
# 面板宽度是写死 340 的，内容 sizeHint 一旦超过它，行内必然有东西被静默切掉。
# 注意：必须 host.show() 之后量——宿主不可见时所有子控件 isVisible() 都是 False，
# 用 isVisible() 过滤会把整个检查跳过，得到假绿（2026-11-10 自己踩到）。
check("panel content fits 340", panel.sizeHint().width() <= 340,
      f"hint={panel.sizeHint().width()}")
# 行数只会增加，面板高度也得留在父窗口里（1280x800 是 AUTOTEST 基准尺寸）
check("panel bottom inside host", panel.y() + panel.height() < host.height(),
      f"bottom={panel.y() + panel.height()} host_h={host.height()}")

import screen_bright as sb  # noqa: E402

_orig_avail = sb.available
# 「亮度方式」行的文案随能力变（背光可用 / 不可用两种），两种都要量：
# AUTOTEST 下 available() 恒为 False，只测一种等于漏掉真机上真正会显示的那条文案
for capable in (True, False):
    sb.available = lambda _c=capable: _c
    panel._sync_impl_row()
    for auto in (True, False):
        panel._sw_auto.setChecked(auto, emit=False)
        panel._sync_bright_rows()
        app.processEvents()
        clipped = []
        for lab in panel.findChildren(QLabel):
            if not lab.isVisibleTo(panel) or not lab.text():
                continue
            need = lab.sizeHint().width()
            if lab.width() < need:
                clipped.append((lab.text()[:24], lab.width(), need))
        check(f"no clipped labels (capable={capable} auto={auto})", not clipped, str(clipped))

        squeezed = []
        for btn in panel.findChildren(QToolButton):
            if not btn.isVisibleTo(panel) or not btn.text():
                continue
            if btn.width() < btn.fontMetrics().horizontalAdvance(btn.text()) + 6:
                squeezed.append((btn.text(), btn.width()))
        check(f"no squeezed buttons (capable={capable} auto={auto})", not squeezed, str(squeezed))
sb.available = _orig_avail

# 能力切换：不支持时必须禁掉「背光」项并勾选「压暗」
sb.available = lambda: False
panel._sync_impl_row()
check("不支持背光时禁用背光项", not panel._impl_btns["backlight"].isEnabled())
check("不支持背光时勾选压暗", panel._impl_btns["overlay"].isChecked())
sb.available = lambda: True
panel._sync_impl_row()
check("支持时背光项可用", panel._impl_btns["backlight"].isEnabled())
sb.available = _orig_avail

print(f"PANEL_FIT pass={PASS} fail={FAIL}  panel={panel.width()}x{panel.height()}")
sys.exit(1 if FAIL else 0)
