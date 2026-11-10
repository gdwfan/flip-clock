"""版本号单一来源。

面板底部显示、打包时写进 exe 版本资源（文件属性）、Release tag 三处都取这里，
避免「改了源码忘了改版本资源」这类不同步。
改版本时同时更新 Rx 说明见 README「版本」小节。
"""

__version__ = "1.1.0"

# exe 版本资源要求四段（FileVersion/ProductVersion）
VERSION_4 = __version__ + ".0"
