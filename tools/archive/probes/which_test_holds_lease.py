# -*- coding: utf-8 -*-
# 定位：哪些测试模块跑完后 profile 租约被占着（导致后续 BrowserBusy）
import sys
import unittest

sys.path.insert(0, '.')

from web_drivers.browser_pool import profile_in_use, live_browsers  # noqa: E402

for name in ('tests.test_author_inject', 'tests.test_author_profiler',
             'tests.test_author_profiler_v2', 'tests.test_automation_core',
             'tests.test_ai_flavor'):
    suite = unittest.TestLoader().loadTestsFromName(name)
    unittest.TextTestRunner(verbosity=0, stream=open('nul', 'w')).run(suite)
    print('%-34s profile_in_use=%s live=%s' % (name, profile_in_use(), live_browsers()))
