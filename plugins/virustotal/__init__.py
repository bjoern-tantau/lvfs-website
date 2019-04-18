#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2019 Richard Hughes <richard@hughsie.com>
# Licensed under the GNU General Public License Version 2
#
# pylint: disable=no-self-use

import requests

from app.pluginloader import PluginBase, PluginError, PluginSettingBool, PluginSettingText
from app.util import _get_absolute_path
from app.models import Test

class Plugin(PluginBase):
    def __init__(self):
        PluginBase.__init__(self)

    def name(self):
        return 'VirusTotal'

    def summary(self):
        return 'Upload firmware to VirusTotal for false-positive detection'

    def settings(self):
        s = []
        s.append(PluginSettingBool('virustotal_enable', 'Enabled', True))
        s.append(PluginSettingText('virustotal_remotes', 'Upload Firmware in Remotes', 'stable,testing'))
        s.append(PluginSettingText('virustotal_api_key', 'API Key', 'DEADBEEF'))
        s.append(PluginSettingText('virustotal_uri', 'Host', 'https://www.virustotal.com/api/v3/monitor/items'))
        s.append(PluginSettingText('virustotal_user_agent', 'User Agent', 'LVFS'))
        return s

    def ensure_test_for_fw(self, fw):

        # is the firmware not in a correct remote
        remotes = self.get_setting('virustotal_remotes', required=True).split(',')
        if fw.remote.name not in remotes:
            return

        # add if not already exists on any component in the firmware
        test = fw.find_test_by_plugin_id(self.id)
        if not test:
            test = Test(self.id)
            fw.tests.append(test)

    def run_test_on_fw(self, test, fw):

        # build the remote name
        fn = _get_absolute_path(fw)
        remote_path = '/' + fw.vendor.group_id + '/' + str(fw.firmware_id) + '/' + fw.filename[41:]

        # upload the file
        try:
            with open(fn, 'rb') as file_obj:
                headers = {}
                headers['X-Apikey'] = self.get_setting('virustotal_api_key', required=True)
                headers['User-Agent'] = self.get_setting('virustotal_user_agent', required=True)
                files = {'file': ('filepath', file_obj, 'application/octet-stream')}
                args = {'path': remote_path}
                r = requests.post(self.get_setting('virustotal_uri', required=True),
                                  files=files, data=args, headers=headers)
                if r.status_code != 200:
                    test.add_fail('Uploading', r.text)
                    return
        except IOError as e:
            raise PluginError(e)

        # success
        test.add_pass('Uploaded', 'All OK')