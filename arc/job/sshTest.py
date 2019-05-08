#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
This module contains unit tests of the arc.job.ssh module
"""

from __future__ import (absolute_import, division, print_function, unicode_literals)
import unittest

import arc.job.ssh as ssh

################################################################################


class TestSSH(unittest.TestCase):
    """
    Contains unit tests for the SSH module
    """

    def test_check_job_status_in_stdout(self):
        """Test checking the job status in stdout"""
        stdout = """job-ID  prior   name       user         state submit/start at     queue                          slots ja-task-ID 
-----------------------------------------------------------------------------------------------------------------
 582682 0.45451 a9654      alongd       e     04/17/2019 16:22:14 long5@node93.cluster              48
 588334 0.45451 pf1005a    alongd       r     05/07/2019 16:24:31 long3@node67.cluster              48
 588345 0.45451 a14121     alongd       r     05/08/2019 02:11:42 long3@node69.cluster              48    """
        status1 = ssh.check_job_status_in_stdout(job_id=588345, stdout=stdout, server='server1')
        self.assertEqual(status1, 'running')
        status2 = ssh.check_job_status_in_stdout(job_id=582682, stdout=stdout, server='server1')
        self.assertEqual(status2, 'errored')
        status3 = ssh.check_job_status_in_stdout(job_id=582600, stdout=stdout, server='server1')
        self.assertEqual(status3, 'done')

################################################################################


if __name__ == '__main__':
    unittest.main(testRunner=unittest.TextTestRunner(verbosity=2))
