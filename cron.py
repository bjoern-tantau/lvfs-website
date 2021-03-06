#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2018 Richard Hughes <richard@hughsie.com>
#
# SPDX-License-Identifier: GPL-2.0+
#
# pylint: disable=singleton-comparison,wrong-import-position

import os
import sys
import hashlib
import datetime
import yara

from flask import render_template

from cabarchive import CabArchive

from lvfs import app, db, ploader
from lvfs.dbutils import _execute_count_star
from lvfs.emails import send_email
from lvfs.models import Remote, Firmware, Vendor, Client, AnalyticVendor, User, YaraQuery, YaraQueryResult
from lvfs.models import AnalyticFirmware, Useragent, UseragentKind, Analytic, Report
from lvfs.models import ComponentShard, ComponentShardInfo, Test, Component, Category, Protocol, FirmwareEvent
from lvfs.models import _get_datestr_from_datetime
from lvfs.metadata.utils import _metadata_update_targets, _metadata_update_pulp
from lvfs.util import _event_log, _get_shard_path, _get_absolute_path
from lvfs.upload.uploadedfile import UploadedFile, MetadataInvalid

def _regenerate_and_sign_metadata(only_embargo=False):

    # get list of dirty remotes
    remotes = []
    for r in db.session.query(Remote):
        if not r.is_signed:
            continue
        # fix up any remotes that are not dirty, but have firmware that is dirty
        # -- which shouldn't happen, but did...
        if not r.is_dirty:
            for fw in r.fws:
                if not fw.is_dirty:
                    continue
                print('Marking remote %s as dirty due to %u' % (r.name, fw.firmware_id))
                r.is_dirty = True
        if r.is_dirty:
            if r.is_public and only_embargo:
                continue
            remotes.append(r)

    # nothing to do
    if not remotes:
        return

    # update everything required
    for r in remotes:
        print('Updating: %s' % r.name)
    _metadata_update_targets(remotes)
    for r in remotes:
        if r.name == 'stable':
            _metadata_update_pulp()

    # sign and sync
    download_dir = app.config['DOWNLOAD_DIR']
    for r in remotes:
        ploader.file_modified(os.path.join(download_dir, r.filename))

    # mark as no longer dirty
    for r in remotes:
        r.is_dirty = False
        db.session.commit()

    # drop caches in other sessions
    db.session.expire_all()

    # log what we did
    for r in remotes:
        _event_log('Signed metadata %s' % r.name)

def _sign_fw(fw):

    # load the .cab file
    download_dir = app.config['DOWNLOAD_DIR']
    fn = os.path.join(download_dir, fw.filename)
    try:
        with open(fn, 'rb') as f:
            cabarchive = CabArchive(f.read())
    except IOError as e:
        raise NotImplementedError('cannot read %s: %s' % (fn, str(e)))

    # sign each component in the archive
    print('Signing: %s' % fn)
    for md in fw.mds:
        try:
            ploader.archive_sign(cabarchive, cabarchive[md.filename_contents])
        except KeyError as _:
            raise NotImplementedError('no {} firmware found'.format(md.filename_contents))

    # overwrite old file
    cab_data = cabarchive.save()
    with open(fn, 'wb') as f:
        f.write(cab_data)

    # inform the plugin loader
    ploader.file_modified(fn)

    # update the download size
    for md in fw.mds:
        md.release_download_size = len(cab_data)

    # update the database
    fw.checksum_signed = hashlib.sha1(cab_data).hexdigest()
    fw.checksum_pulp = hashlib.sha256(cab_data).hexdigest()
    fw.signed_timestamp = datetime.datetime.utcnow()
    db.session.commit()

def _repair():

    # fix any timestamps that are incorrect
    for md in db.session.query(Component).filter(Component.release_timestamp < 1980):
        fn = _get_absolute_path(md.fw)
        print(fn, md.release_timestamp)
        try:
            ufile = UploadedFile(is_strict=False)
            for cat in db.session.query(Category):
                ufile.category_map[cat.value] = cat.category_id
            for pro in db.session.query(Protocol):
                ufile.protocol_map[pro.value] = pro.protocol_id
            with open(fn, 'rb') as f:
                ufile.parse(os.path.basename(fn), f.read())
        except MetadataInvalid as e:
            print('failed to parse file: {}'.format(str(e)))
            continue
        for md_local in ufile.fw.mds:
            if md_local.appstream_id == md.appstream_id:
                print('repairing timestamp from {} to {}'.format(md.release_timestamp,
                                                                 md_local.release_timestamp))
                md.release_timestamp = md_local.release_timestamp
                md.fw.mark_dirty()

    # fix all the checksums and file sizes
    for fw in db.session.query(Firmware):
        try:
            with open(fw.filename_absolute, 'rb') as f:
                checksum_pulp = hashlib.sha256(f.read()).hexdigest()
                if checksum_pulp != fw.checksum_pulp:
                    print('repairing checksum from {} to {}'.format(fw.checksum_pulp,
                                                                    checksum_pulp))
                    fw.checksum_pulp = checksum_pulp
                    fw.mark_dirty()
            for md in fw.mds:
                sz = os.path.getsize(fw.filename_absolute)
                if sz != md.release_download_size:
                    print('repairing size from {} to {}'.format(md.release_download_size, sz))
                    md.release_download_size = sz
                    md.fw.mark_dirty()
        except FileNotFoundError as _:
            pass

        # ensure the test has been added for the firmware type
        if not fw.is_deleted:
            ploader.ensure_test_for_fw(fw)

    # all done
    db.session.commit()

def _regenerate_and_sign_firmware():

    # find all unsigned firmware
    fws = db.session.query(Firmware).\
                        filter(Firmware.signed_timestamp == None).all()
    if not fws:
        return

    # sign each firmware in each file
    for fw in fws:
        if fw.is_deleted:
            continue
        print('Signing firmware %u...' % fw.firmware_id)
        _sign_fw(fw)
        _event_log('Signed firmware %s' % fw.firmware_id)

    # drop caches in other sessions
    db.session.expire_all()

def _purge_old_deleted_firmware():

    # find all unsigned firmware
    for fw in db.session.query(Firmware).\
                    join(Remote).filter(Remote.name == 'deleted'):
        if fw.target_duration > datetime.timedelta(days=30*6):
            print('Deleting %s as age %s' % (fw.filename, fw.target_duration))
            path = os.path.join(app.config['RESTORE_DIR'], fw.filename)
            if os.path.exists(path):
                os.remove(path)
            for md in fw.mds:
                for shard in md.shards:
                    path = _get_shard_path(shard)
                    if os.path.exists(path):
                        os.remove(path)
            db.session.delete(fw)

    # all done
    db.session.commit()

def _test_priority_sort_func(test):
    plugin = ploader.get_by_id(test.plugin_id)
    return plugin.priority

def _yara_query_shard(query, md, shard):
    if not shard.blob:
        return
    matches = query.rules.match(data=shard.blob)
    for match in matches:
        msg = match.rule
        for string in match.strings:
            if len(string) == 3:
                try:
                    msg += ': found {}'.format(string[2].decode())
                except UnicodeDecodeError as _:
                    pass
        query.results.append(YaraQueryResult(md=md, shard=shard, result=msg))

    # unallocate the cached blob as it's no longer needed
    shard.blob = None

def _yara_query_component(query, md):
    if not md.blob:
        return
    matches = query.rules.match(data=md.blob)
    for match in matches:
        msg = match.rule
        for string in match.strings:
            if len(string) == 3:
                try:
                    msg += ': found {}'.format(string[2].decode())
                except UnicodeDecodeError as _:
                    pass
        query.results.append(YaraQueryResult(md=md, result=msg))

    # unallocate the cached blob as it's no longer needed
    md.blob = None

def _yara_query_all():

    # get all pending queries
    pending = db.session.query(YaraQuery).\
                    filter(YaraQuery.started_ts == None).\
                    filter(YaraQuery.error == None)
    if not pending:
        return

    # get all stable firmware
    firmware = db.session.query(Firmware).\
                    join(Remote).filter(Remote.name == 'stable').all()
    for query in pending:
        print('processing query {}: {}...'.format(query.yara_query_id, query.title))
        try:
            query.rules = yara.compile(source=query.value)
        except yara.SyntaxError as e:
            query.error = 'Failed to compile rules: {}'.format(str(e))
            db.session.commit()
            continue
        query.started_ts = datetime.datetime.utcnow()
        db.session.commit()
        for fw in firmware:
            for md in fw.mds:
                for shard in md.shards:
                    _yara_query_shard(query, md, shard)
                _yara_query_component(query, md)
                query.total += len(md.shards)
        query.found = len(query.results)
        query.ended_ts = datetime.datetime.utcnow()
        db.session.commit()

def _check_firmware():

    # make a list of all the tests that need running
    tests = db.session.query(Test).filter(Test.started_ts == None).all()

    # mark all the tests as started
    for test in tests:
        print('Marking test {} started for firmware {}...'.format(test.plugin_id, test.fw.firmware_id))
        test.started_ts = datetime.datetime.utcnow()
    db.session.commit()

    # process each test
    for test in sorted(tests, key=_test_priority_sort_func):
        plugin = ploader.get_by_id(test.plugin_id)
        if not plugin:
            _event_log('No plugin %s' % test.plugin_id)
            test.ended_ts = datetime.datetime.utcnow()
            continue
        if not hasattr(plugin, 'run_test_on_fw'):
            _event_log('No run_test_on_fw in %s' % test.plugin_id)
            test.ended_ts = datetime.datetime.utcnow()
            continue
        try:
            print('Running test {} for firmware {}'.format(test.plugin_id, test.fw.firmware_id))
            plugin.run_test_on_fw(test, test.fw)
            test.ended_ts = datetime.datetime.utcnow()
            # don't leave a failed task running
            db.session.commit()
        except Exception as e: # pylint: disable=broad-except
            test.ended_ts = datetime.datetime.utcnow()
            test.add_fail('An exception occurred', str(e))

    # all done
    db.session.commit()


def _generate_stats_for_vendor(v, datestr):

    # is datestr older than firmware
    if not v.ctime:
        return
    if datestr < _get_datestr_from_datetime(v.ctime - datetime.timedelta(days=1)):
        return

    # get all the firmware for a specific vendor
    fw_ids = [fw.firmware_id for fw in v.fws]
    if not fw_ids:
        return

    # count how many times any of the firmware files were downloaded
    cnt = _execute_count_star(db.session.query(Client).\
                    filter(Client.firmware_id.in_(fw_ids)).\
                    filter(Client.datestr == datestr))
    analytic = AnalyticVendor(v.vendor_id, datestr, cnt)
    print('adding %s:%s = %i' % (datestr, v.group_id, cnt))
    db.session.add(analytic)

def _generate_stats_for_firmware(fw, datestr):

    # is datestr older than firmware
    if datestr < _get_datestr_from_datetime(fw.timestamp):
        return

    # count how many times any of the firmware files were downloaded
    cnt = _execute_count_star(db.session.query(Client).\
                    filter(Client.firmware_id == fw.firmware_id).\
                    filter(Client.datestr == datestr))
    analytic = AnalyticFirmware(fw.firmware_id, datestr, cnt)
    db.session.add(analytic)

def _demote_back_to_testing(fw):

    # from the server admin
    user = db.session.query(User).filter(User.username == 'anon@fwupd.org').first()
    if not user:
        return

    # send email to uploading user
    if fw.user.get_action('notify-demote-failures'):
        send_email("[LVFS] Firmware has been demoted",
                   fw.user.email_address,
                   render_template('email-firmware-demote.txt',
                                   user=fw.user, fw=fw))

    fw.mark_dirty()
    remote = db.session.query(Remote).filter(Remote.name == 'testing').first()
    remote.is_dirty = True
    fw.remote_id = remote.remote_id
    fw.events.append(FirmwareEvent(fw.remote_id, user_id=user.user_id))
    db.session.commit()
    _event_log('Demoted firmware {} as reported success {}%%'.format(fw.firmware_id, fw.success))

def _generate_stats_firmware_reports(fw):

    # count how many times any of the firmware files were downloaded
    reports_success = 0
    reports_failure = 0
    reports_issue = 0
    for r in db.session.query(Report).\
                    filter(Report.firmware_id == fw.firmware_id,
                           Report.timestamp > datetime.date.today() - datetime.timedelta(weeks=26)):
        if r.state == 2:
            reports_success += 1
        if r.state == 3:
            if r.issue_id:
                reports_issue += 1
            else:
                reports_failure += 1

    # update
    fw.report_success_cnt = reports_success
    fw.report_failure_cnt = reports_failure
    fw.report_issue_cnt = reports_issue

    # check the limits and demote back to embargo if required
    if fw.remote.name == 'stable' and fw.is_failure:
        _demote_back_to_testing(fw)

def _get_app_from_ua(ua):
    # always exists
    return ua.split(' ')[0]

def _get_fwupd_from_ua(ua):
    for part in ua.split(' '):
        if part.startswith('fwupd/'):
            return part[6:]
    return 'Unknown'

def _get_lang_distro_from_ua(ua):
    start = ua.find('(')
    end = ua.rfind(')')
    if start == -1 or end == -1:
        return None
    parts = ua[start+1:end].split('; ')
    if len(parts) != 3:
        return None
    return (parts[1], parts[2])

def _generate_stats_shard_info(info):
    if info.cnt != len(info.shards):
        print('fixing ComponentShardInfo %i: %i -> %i' % (info.component_shard_info_id, info.cnt, len(info.shards)))
        info.cnt = len(info.shards)

def _generate_stats(kinds=None):
    if not kinds:
        kinds = ['FirmwareReport', 'ShardCount', 'ShardInfo']

    # Set ComponentShardInfo in ComponentShard if GUID matches
    if 'ShardInfo' in kinds:
        for shard in db.session.query(ComponentShard).\
                            filter(ComponentShard.component_shard_info_id == None):
            shard.info = db.session.query(ComponentShardInfo).\
                            filter(ComponentShardInfo.guid == shard.guid).first()
            if not shard.info:
                print('creating ComponentShardInfo for {}'.format(shard.guid))
                shard.info = ComponentShardInfo(guid=shard.guid)
        db.session.commit()

    # update ComponentShardInfo.cnt
    if 'ShardCount' in kinds:
        for info in db.session.query(ComponentShardInfo):
            _generate_stats_shard_info(info)
        db.session.commit()

    # update FirmwareReport counts
    if 'FirmwareReport' in kinds:
        for fw in db.session.query(Firmware):
            _generate_stats_firmware_reports(fw)
        db.session.commit()

    print('generated %s' % ','.join(kinds))

def _generate_stats_for_datestr(datestr, kinds=None):

    if not kinds:
        kinds = ['Analytic',
                 'AnalyticVendor',
                 'AnalyticFirmware',
                 'Useragent']

    # update AnalyticVendor
    if 'AnalyticVendor' in kinds:
        for analytic in db.session.query(AnalyticVendor).filter(AnalyticVendor.datestr == datestr):
            db.session.delete(analytic)
        db.session.commit()
        for v in db.session.query(Vendor):
            _generate_stats_for_vendor(v, datestr)
        db.session.commit()

    # update AnalyticFirmware
    if 'AnalyticFirmware' in kinds:
        for analytic in db.session.query(AnalyticFirmware).filter(AnalyticFirmware.datestr == datestr):
            db.session.delete(analytic)
        db.session.commit()
        for fw in db.session.query(Firmware):
            _generate_stats_for_firmware(fw, datestr)
        db.session.commit()

    # update Useragent
    if 'Useragent' in kinds:
        for agnt in db.session.query(Useragent).filter(Useragent.datestr == datestr):
            db.session.delete(agnt)
        db.session.commit()
        ua_apps = {}
        ua_fwupds = {}
        ua_distros = {}
        ua_langs = {}
        clients = db.session.query(Client.user_agent).\
                        filter(Client.datestr == datestr).all()
        for res in clients:
            ua = res[0]
            if not ua:
                continue

            # downloader app
            ua_app = _get_app_from_ua(ua)
            if ua_app not in ua_apps:
                ua_apps[ua_app] = 1
            else:
                ua_apps[ua_app] += 1

            # fwupd version
            ua_fwupd = _get_fwupd_from_ua(ua)
            if ua_fwupd not in ua_fwupds:
                ua_fwupds[ua_fwupd] = 1
            else:
                ua_fwupds[ua_fwupd] += 1

            # language and distro
            ua_lang_distro = _get_lang_distro_from_ua(ua)
            if ua_lang_distro:
                ua_lang = ua_lang_distro[0]
                ua_distro = ua_lang_distro[1]
                if ua_lang not in ua_langs:
                    ua_langs[ua_lang] = 1
                else:
                    ua_langs[ua_lang] += 1
                if ua_distro not in ua_distros:
                    ua_distros[ua_distro] = 1
                else:
                    ua_distros[ua_distro] += 1
        for ua in ua_apps:
            db.session.add(Useragent(UseragentKind.APP, ua, datestr, cnt=ua_apps[ua]))
        for ua in ua_fwupds:
            db.session.add(Useragent(UseragentKind.FWUPD, ua, datestr, cnt=ua_fwupds[ua]))
        for ua in ua_langs:
            db.session.add(Useragent(UseragentKind.LANG, ua, datestr, cnt=ua_langs[ua]))
        for ua in ua_distros:
            db.session.add(Useragent(UseragentKind.DISTRO, ua, datestr, cnt=ua_distros[ua]))
        db.session.commit()

    # update Analytic
    if 'Analytic' in kinds:
        analytic = db.session.query(Analytic).filter(Analytic.datestr == datestr).first()
        if analytic:
            db.session.delete(analytic)
            db.session.commit()
        db.session.add(Analytic(datestr, len(clients)))
        db.session.commit()

    # for the log
    print('generated for %s: %s' % (datestr, ','.join(kinds)))

if __name__ == '__main__':

    if len(sys.argv) < 2:
        print('Usage: %s [metadata] [firmware]' % sys.argv[0])
        sys.exit(1)

    # regenerate and sign firmware then metadata
    if 'repair' in sys.argv:
        try:
            with app.test_request_context():
                _repair()
        except NotImplementedError as e:
            print(str(e))
            sys.exit(1)
    if 'firmware' in sys.argv:
        try:
            with app.test_request_context():
                _regenerate_and_sign_firmware()
        except NotImplementedError as e:
            print(str(e))
            sys.exit(1)
    if 'metadata' in sys.argv:
        _only_embargo = False
        if len(sys.argv) > 2:
            for kind in sys.argv[2:]:
                if kind == 'embargo':
                    _only_embargo = True
        try:
            with app.test_request_context():
                _regenerate_and_sign_metadata(only_embargo=_only_embargo)
        except NotImplementedError as e:
            print(str(e))
            sys.exit(1)
    if 'purgedelete' in sys.argv:
        try:
            with app.test_request_context():
                _purge_old_deleted_firmware()
        except NotImplementedError as e:
            print(str(e))
            sys.exit(1)
    if 'fwchecks' in sys.argv:
        try:
            with app.test_request_context():
                _check_firmware()
                _yara_query_all()
        except NotImplementedError as e:
            print(str(e))
            sys.exit(1)
    if 'stats' in sys.argv:
        try:
            with app.test_request_context():
                # default to yesterday, but also allow specifying the offset
                days = 1
                if len(sys.argv) > 2:
                    days = int(sys.argv[2])
                val = _get_datestr_from_datetime(datetime.date.today() - datetime.timedelta(days=days))
                _generate_stats_for_datestr(val)
                _generate_stats()
        except NotImplementedError as e:
            print(str(e))
            sys.exit(1)
    if 'statsmigrate' in sys.argv:
        try:
            update_kinds = None
            if len(sys.argv) > 2:
                update_kinds = sys.argv[2:]
            with app.test_request_context():
                for days in range(1, 720):
                    val = _get_datestr_from_datetime(datetime.date.today() - datetime.timedelta(days=days))
                    _generate_stats_for_datestr(val, kinds=update_kinds)
        except NotImplementedError as e:
            print(str(e))
            sys.exit(1)

    # success
    sys.exit(0)
