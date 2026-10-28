"""aviary:类 magpie 的本地 LLM 网关。

参考架构: yetone/magpie (https://github.com/yetone/magpie) —— 一个本地网关,
所有 agent 指向它,它再把请求路由/翻译到任意上游 provider,并在配额耗尽或
上游报错时自动切换 [yetone/magpie README]。
"""

__version__ = "0.2.1"
