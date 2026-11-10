"""打包脚本（仅 onedir，交付动作，不是验证动作）。

按 AGENTS.md：开发/调试一律跑源码；只有用户明确要「出包/打包」时才执行本脚本。

关键约束：
- 输出目录必须是 ASCII 路径（中文路径会打乱子进程编码），故先出到 C:/scratch，
  成功后再拷回项目 dist/。
- 打包前结束正在运行的旧 exe（否则新产物写不进去）。
- 判据只看退出码：非 0 一律不信产物。

跑法：py -3.10 -X utf8 build_app.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCREENSHOT_SAFE = Path("C:/scratch/clock-dist")  # ASCII 暂存
FINAL = ROOT / "dist"

# 排除用不到的 Qt 模块压体积（QtNetwork 必须保留：单实例用 QLocalServer/QLocalSocket）
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPrintSupport", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtSerialPort", "PySide6.QtWebSockets",
    "PySide6.QtWebChannel", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtTextToSpeech", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtStateMachine",
    "tkinter", "unittest", "pydoc_data", "PIL",
]


def _write_version_file(path: Path) -> Path:
    """生成 exe 版本资源（文件属性里显示的版本号/产品名）。

    保持纯 ASCII：PyInstaller 解析 version file 时对非 ASCII 字面量不稳，
    中文产品名在 Windows 文件属性里也易出现乱码，故用英文名。
    """
    path.write_text(
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(filevers=(1,0,0,0), prodvers=(1,0,0,0),\n"
        "    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),\n"
        "  kids=[\n"
        "    StringFileInfo([StringTable('040904B0', [\n"
        "      StringStruct('CompanyName', 'FlipClock'),\n"
        "      StringStruct('FileDescription', 'FlipClock - desktop flip clock'),\n"
        "      StringStruct('FileVersion', '1.0.0.0'),\n"
        "      StringStruct('InternalName', 'FlipClock'),\n"
        "      StringStruct('OriginalFilename', 'FlipClock.exe'),\n"
        "      StringStruct('ProductName', 'FlipClock'),\n"
        "      StringStruct('ProductVersion', '1.0.0.0')])]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n",
        encoding="utf-8",
    )
    return path


# 构建后瘦身：--exclude-module 只能挡 Python 模块，Qt 的 DLL/插件/翻译要手工清。
# 每一项都是「本程序确定不用」的体系，删完必须过 packaged_smoke（删错即启动崩）。
SLIM_FILES = [
    # —— Qt 的 GL / QML / PDF 体系（本程序纯 raster 自绘，实测 37.5MB）——
    "PySide6/opengl32sw.dll",   # 软件 OpenGL(Mesa)，19.7MB
    "PySide6/Qt6Quick.dll",     # QML Quick 运行时
    "PySide6/Qt6Qml.dll",
    "PySide6/Qt6Pdf.dll",
    "PySide6/Qt6OpenGL.dll",
    # —— TLS 后端（NTP 走 UDP，兜底走明文 HTTP Date，全程无 TLS）——
    "PySide6/plugins/tls/qschannelbackend.dll",
    "PySide6/plugins/tls/qopensslbackend.dll",
    "PySide6/plugins/tls/qcertonlybackend.dll",
    # —— 图片格式插件：只留 qico（应用图标是 .ico），其余格式不读也不写 ——
    "PySide6/plugins/imageformats/qgif.dll",
    "PySide6/plugins/imageformats/qicns.dll",
    "PySide6/plugins/imageformats/qjpeg.dll",
    "PySide6/plugins/imageformats/qpdf.dll",
    "PySide6/plugins/imageformats/qsvg.dll",
    "PySide6/plugins/imageformats/qtga.dll",
    "PySide6/plugins/imageformats/qtiff.dll",
    "PySide6/plugins/imageformats/qwbmp.dll",
    "PySide6/plugins/imageformats/qwebp.dll",
    # —— 界面无文本输入，不需要输入法插件 ——
    "PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
    # —— 平台插件只留 qwindows（其余是 offscreen/minimal/嵌入场景）——
    "PySide6/plugins/platforms/qoffscreen.dll",
    "PySide6/plugins/platforms/qminimal.dll",
    "PySide6/plugins/platforms/qdirect2d.dll",
]

# Qt 自带翻译（6.9MB）：全部界面文案都是自绘/自写中文，Qt 内置文案（按钮、对话框）本程序不出现
SLIM_DIRS = ["PySide6/translations"]

# 明确保留（删了会出问题，别再"优化"掉）：
#   Qt6Network + plugins/networkinformation  → 单实例锁 QLocalServer/QLocalSocket 依赖
#   plugins/platforms/qwindows.dll           → 唯一的窗口后端
#   plugins/styles/qmodernwindowsstyle.dll   → 设置面板控件外观
#   plugins/imageformats/qico.dll            → 应用图标 .ico 读取
#   libcrypto/libssl + _hashlib.pyd          → 与 _hashlib 同生共死，收益不抵风险


def _slim(dist_dir: Path) -> float:
    """删除 SLIM 清单项，返回释放的字节数。"""
    freed = 0
    for rel in SLIM_FILES:
        for cand in (dist_dir / "_internal" / rel, dist_dir / rel):
            try:
                if cand.is_file():
                    freed += cand.stat().st_size
                    cand.unlink()
                    break
            except OSError:
                pass
    for rel in SLIM_DIRS:
        for cand in (dist_dir / "_internal" / rel, dist_dir / rel):
            if cand.is_dir():
                freed += sum(f.stat().st_size for f in cand.rglob("*") if f.is_file())
                shutil.rmtree(cand, ignore_errors=True)
                break
    return freed


def main():
    if SCREENSHOT_SAFE.exists():
        shutil.rmtree(SCREENSHOT_SAFE, ignore_errors=True)
    work = Path("C:/scratch/clock-build")
    work.mkdir(parents=True, exist_ok=True)
    vfile = _write_version_file(work / "version_info.txt")
    args = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "src" / "main.py"),
        "--name", "FlipClock",
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--icon", str(ROOT / "assets" / "icon.ico"),
        "--version-file", str(vfile),
        "--add-data", f"{ROOT / 'assets' / 'icon.ico'};assets",
        "--distpath", str(SCREENSHOT_SAFE),
        "--workpath", str(work),
        "--specpath", str(work),
    ]
    for m in EXCLUDES:
        args += ["--exclude-module", m]
    r = subprocess.run(args, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"BUILD FAILED exit={r.returncode}")
        return r.returncode
    src_dir = SCREENSHOT_SAFE / "FlipClock"
    if not (src_dir / "FlipClock.exe").exists():
        print("BUILD FAILED: FlipClock.exe not found in output")
        return 1
    FINAL.mkdir(exist_ok=True)
    dst = FINAL / "FlipClock"
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src_dir, dst)
    freed = _slim(dst)
    total = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    print(f"SLIM freed {freed / 1048576:.1f} MB")
    print(f"BUILD OK -> {dst}  ({total / 1048576:.1f} MB)")
    print("NEXT: py -3.10 -X utf8 _probe/packaged_smoke.py   # 删错就会在这里暴露")
    return 0


if __name__ == "__main__":
    sys.exit(main())
