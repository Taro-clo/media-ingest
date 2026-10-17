"""媒体库硬链接管理器（后端包）。

执行体只有两个，都是本项目自己的代码：硬链接走
``app/processors/hardlink.py``，重命名走配置驱动的 ``app/processors/engine.py``；
行为由项目自己的回归测试守卫。
"""

__version__ = "1.0.0"
