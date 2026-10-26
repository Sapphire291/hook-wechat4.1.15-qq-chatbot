# -*- coding: utf-8 -*-
"""策略层：白名单控制 + 回复频率策略。

对外暴露：
- :class:`WhitelistPolicyImpl` / :class:`FrequencyPolicyImpl` 两个实现类；
- :func:`create_whitelist_policy` / :func:`create_frequency_policy` 两个工厂函数。
"""
from .frequency import FrequencyPolicyImpl, create_frequency_policy
from .whitelist import WhitelistPolicyImpl, create_whitelist_policy

__all__ = [
    "WhitelistPolicyImpl",
    "FrequencyPolicyImpl",
    "create_whitelist_policy",
    "create_frequency_policy",
]
