#!/usr/bin/python

import warnings
# Dropping a table inexplicably produces a warning despite
# the "IF EXISTS" clause. Squelch these warnings.
warnings.simplefilter("ignore")

from optparse import OptionParser
import os
import shutil
import socket
from subprocess import PIPE, call
import sys
import time

import MySQLdb

import utils
import tablet

devnull = open('/dev/null', 'w')
vttop = os.environ['VTTOP']
vtroot = os.environ['VTROOT']
hostname = socket.gethostname()

tablet_62344 = tablet.Tablet(62344, 6700, 3700)
tablet_62044 = tablet.Tablet(62044, 6701, 3701)
tablet_41983 = tablet.Tablet(41983, 6702, 3702)
tablet_31981 = tablet.Tablet(31981, 6703, 3703)

def setup():
  utils.zk_setup()

  # start mysql instance external to the test
  setup_procs = [
      tablet_62344.start_mysql(),
      tablet_62044.start_mysql(),
      tablet_41983.start_mysql(),
      tablet_31981.start_mysql(),
      ]
  utils.wait_procs(setup_procs)

def teardown():
  if utils.options.skip_teardown:
    return

  teardown_procs = [
      tablet_62344.teardown_mysql(),
      tablet_62044.teardown_mysql(),
      tablet_41983.teardown_mysql(),
      tablet_31981.teardown_mysql(),
      ]
  utils.wait_procs(teardown_procs, raise_on_error=False)

  utils.zk_teardown()
  utils.kill_sub_processes()
  utils.remove_tmp_files()

  tablet_62344.remove_tree()
  tablet_62044.remove_tree()
  tablet_41983.remove_tree()
  tablet_31981.remove_tree()

  for path in ['/vt/snapshot']:
    try:
      shutil.rmtree(path)
    except OSError as e:
      if utils.options.verbose:
        print >> sys.stderr, e, path

def _check_db_addr(db_addr, expected_addr):
  # Run in the background to capture output.
  proc = utils.run_bg(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=WARNING -zk.local-cell=test_nj Resolve ' + db_addr, stdout=PIPE)
  stdout = proc.communicate()[0].strip()
  if stdout != expected_addr:
    raise utils.TestError('wrong zk address', db_addr, stdout, expected_addr)


@utils.test_case
def run_test_sanity():
  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')
  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('RebuildKeyspaceGraph /zk/global/vt/keyspaces/test_keyspace')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  # if these statements don't run before the tablet it will wedge waiting for the
  # db to become accessible. this is more a bug than a feature.
  tablet_62344.populate('vt_test_keyspace', create_vt_select_test,
                        populate_vt_select_test)

  tablet_62344.start_vttablet()

  # make sure the query service is started right away
  result, _ = utils.run_vtctl('Query /zk/test_nj/vt/ns/test_keyspace "select * from vt_select_test"', trap_output=True)
  rows = result.splitlines()
  if len(rows) != 5:
    raise utils.TestError("expected 5 rows in vt_select_test", rows, result)

  # check Ping
  utils.run_vtctl('Ping ' + tablet_62344.zk_tablet_path)

  # Quickly check basic actions.
  utils.run_vtctl('SetReadOnly ' + tablet_62344.zk_tablet_path)
  utils.wait_db_read_only(62344)

  utils.run_vtctl('SetReadWrite ' + tablet_62344.zk_tablet_path)
  utils.check_db_read_write(62344)

  utils.run_vtctl('DemoteMaster ' + tablet_62344.zk_tablet_path)
  utils.wait_db_read_only(62344)

  utils.run_vtctl('Validate /zk/global/vt/keyspaces')
  utils.run_vtctl('ValidateKeyspace /zk/global/vt/keyspaces/test_keyspace')
  utils.run_vtctl('ValidateShard /zk/global/vt/keyspaces/test_keyspace/shards/0')

  tablet_62344.kill_vttablet()

  tablet_62344.init_tablet('idle')
  utils.run_vtctl('ScrapTablet -force ' + tablet_62344.zk_tablet_path)


@utils.test_case
def run_test_scrap():
  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  tablet_62044.init_tablet('replica', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  utils.run_vtctl('ScrapTablet -force ' + tablet_62044.zk_tablet_path)
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')


create_vt_insert_test = '''create table vt_insert_test (
id bigint auto_increment,
msg varchar(64),
primary key (id)
) Engine=InnoDB'''

populate_vt_insert_test = [
    "insert into vt_insert_test (msg) values ('test %s')" % x
    for x in xrange(4)]

# the varbinary test uses 10 bytes long ids, stored in a 64 bytes column
# (using 10 bytes so it's longer than a uint64 8 bytes)
create_vt_insert_test_varbinary = '''create table vt_insert_test (
id varbinary(64),
msg varchar(64),
primary key (id)
) Engine=InnoDB'''

populate_vt_insert_test_varbinary = [
    "insert into vt_insert_test (id, msg) values (0x%02x000000000000000000, 'test %s')" % (x+1, x+1)
    for x in xrange(4)]

create_vt_select_test = '''create table vt_select_test (
id bigint auto_increment,
msg varchar(64),
primary key (id)
) Engine=InnoDB'''

populate_vt_select_test = [
    "insert into vt_select_test (msg) values ('test %s')" % x
    for x in xrange(4)]


@utils.test_case
def run_test_mysqlctl_clone():
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/snapshot_test')

  tablet_62344.init_tablet('master', 'snapshot_test', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/snapshot_test/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_snapshot_test', create_vt_insert_test,
                        populate_vt_insert_test)

  tablet_62344.start_vttablet()

  err = tablet_62344.mysqlctl('-port 6700 -mysql-port 3700 snapshot -compress-concurrency=5 vt_snapshot_test').wait()
  if err != 0:
    raise utils.TestError('mysqlctl snapshot failed')

  utils.pause("snapshot finished")

  call(["touch", "/tmp/vtSimulateFetchFailures"])
  err = tablet_62044.mysqlctl('-port 6701 -mysql-port 3701 restore -fetch-concurrency=2 -fetch-retry-count=4 /vt/snapshot/vt_0000062344/snapshot_manifest.json').wait()
  if err != 0:
    raise utils.TestError('mysqlctl restore failed')

  tablet_62044.assert_table_count('vt_snapshot_test', 'vt_insert_test', 4)

  tablet_62344.kill_vttablet()

@utils.test_case
def run_test_vtctl_snapshot_restore():
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/snapshot_test')

  tablet_62344.init_tablet('master', 'snapshot_test', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/snapshot_test/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_snapshot_test', create_vt_insert_test,
                        populate_vt_insert_test)

  tablet_62344.start_vttablet()

  # Need to force snapshot since this is a master db.
  out, err = utils.run_vtctl('Snapshot -force -compress-concurrency=4 ' + tablet_62344.zk_tablet_path, log_level='INFO', trap_output=True)
  errPos = err.find("Manifest: ")
  if errPos == -1:
    raise utils.TestError("Snapshot didn't echo Manifest file", err)
  manifest = err[errPos+10:].splitlines()[0]
  errPos = err.find("ParentPath: ")
  if errPos == -1:
    raise utils.TestError("Snapshot didn't echo ParentPath", err)
  parentPath = err[errPos+12:].splitlines()[0]
  utils.pause("snapshot finished: " + manifest + " " + parentPath)

  tablet_62044.init_tablet('idle', start=True)

  # do not specify a MANIFEST, see if default works
  call(["touch", "/tmp/vtSimulateFetchFailures"])
  utils.run_vtctl('Restore -fetch-concurrency=2 -fetch-retry-count=4 %s default %s %s' %
                  (tablet_62344.zk_tablet_path,
                   tablet_62044.zk_tablet_path, parentPath), log_level='INFO')
  utils.pause("restore finished")

  tablet_62044.assert_table_count('vt_snapshot_test', 'vt_insert_test', 4)

  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()


@utils.test_case
def run_test_vtctl_clone():
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/snapshot_test')

  tablet_62344.init_tablet('master', 'snapshot_test', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/snapshot_test/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_snapshot_test', create_vt_insert_test,
                        populate_vt_insert_test)
  tablet_62344.start_vttablet()

  tablet_62044.create_db('vt_snapshot_test')
  tablet_62044.init_tablet('idle', start=True)

  # small test to make sure the directory validation works
  utils.run("mkdir -p /vt/snapshot")
  utils.run("chmod -w /vt/snapshot")
  out, err = utils.run(vtroot+'/bin/vtctl -logfile=/dev/null Clone -force %s %s' %
                       (tablet_62344.zk_tablet_path,
                        tablet_62044.zk_tablet_path),
                       trap_output=True, raise_on_error=False)
  if err.find("Cannot validate snapshot directory") == -1:
    raise utils.TestError("expected validation error", err)
  utils.run("chmod +w /vt/snapshot")

  call(["touch", "/tmp/vtSimulateFetchFailures"])
  utils.run_vtctl('Clone -force %s %s' %
                  (tablet_62344.zk_tablet_path, tablet_62044.zk_tablet_path))

  utils.pause("look at logs!")

  tablet_62044.assert_table_count('vt_snapshot_test', 'vt_insert_test', 4)

  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()

@utils.test_case
def run_test_mysqlctl_split():
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_test_keyspace', create_vt_insert_test,
                        populate_vt_insert_test)

  tablet_62344.start_vttablet()

  err = tablet_62344.mysqlctl('-port 6700 -mysql-port 3700 partialsnapshot -end=0000000000000003 vt_test_keyspace id').wait()
  if err != 0:
    raise utils.TestError('mysqlctl partialsnapshot failed')


  utils.pause("partialsnapshot finished")

  tablet_62044.mquery('', 'stop slave')
  tablet_62044.create_db('vt_test_keyspace')
  call(["touch", "/tmp/vtSimulateFetchFailures"])
  err = tablet_62044.mysqlctl('-port 6701 -mysql-port 3701 partialrestore /vt/snapshot/vt_0000062344/data/vt_test_keyspace-,0000000000000003/partial_snapshot_manifest.json').wait()
  if err != 0:
    raise utils.TestError('mysqlctl partialrestore failed')

  tablet_62044.assert_table_count('vt_test_keyspace', 'vt_insert_test', 2)

  # change/add two values on the master, one in range, one out of range, make
  # sure the right one propagate and not the other
  utils.run_vtctl('SetReadWrite ' + tablet_62344.zk_tablet_path)
  tablet_62344.mquery('vt_test_keyspace', "insert into vt_insert_test (id, msg) values (5, 'test should not propagate')", write=True)
  tablet_62344.mquery('vt_test_keyspace', "update vt_insert_test set msg='test should propagate' where id=2", write=True)

  utils.pause("look at db now!")

  # wait until value that should have been changed is here
  timeout = 10
  while timeout > 0:
    result = tablet_62044.mquery('vt_test_keyspace', 'select msg from vt_insert_test where id=2')
    if result[0][0] == "test should propagate":
      break
    timeout -= 1
    time.sleep(1)
  if timeout == 0:
    raise utils.TestError("expected propagation to happen", result)

  # test value that should not propagate
  # this part is disabled now, as the replication pruning is only enabled
  # for row-based replication, but the mysql server is statement based.
  # will re-enable once we get statement-based pruning patch into mysql.
#  tablet_62044.assert_table_count('vt_test_keyspace', 'vt_insert_test', 0, 'where id=5')

  tablet_62344.kill_vttablet()

def _run_test_vtctl_partial_clone(create, populate,
                                  start, end):
  utils.zk_wipe()

  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/snapshot_test')

  tablet_62344.init_tablet('master', 'snapshot_test', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/snapshot_test/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_snapshot_test', create, populate)

  tablet_62344.start_vttablet()

  tablet_62044.init_tablet('idle', start=True)

  # FIXME(alainjobart): not sure where the right place for this is,
  # but it doesn't seem it should right here. It should be either in
  # InitTablet (running an action on the vttablet), or in PartialClone
  # (instead of doing a 'USE dbname' it could do a 'CREATE DATABASE
  # dbname').
  tablet_62044.mquery('', 'stop slave')
  tablet_62044.create_db('vt_snapshot_test')
  call(["touch", "/tmp/vtSimulateFetchFailures"])
  utils.run_vtctl('PartialClone -force %s %s id %s %s' %
                  (tablet_62344.zk_tablet_path, tablet_62044.zk_tablet_path,
                   start, end))

  utils.pause("after PartialClone")

  # grab the new tablet definition from zk, make sure the start and
  # end keys are set properly
  out, err = utils.run(vtroot+'/bin/zk cat ' + tablet_62044.zk_tablet_path,
                       trap_output=True)
  if (out.find('"Start": "%s"' % start) == -1 or \
        out.find('"End": "%s"' % end) == -1):
    print "Tablet output:"
    print "out"
    raise utils.TestError('wrong Start or End')

  tablet_62044.assert_table_count('vt_snapshot_test', 'vt_insert_test', 2)

  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()

@utils.test_case
def run_test_vtctl_partial_clone():
  _run_test_vtctl_partial_clone(create_vt_insert_test,
                                populate_vt_insert_test,
                                '0000000000000000',
                                '0000000000000003')

@utils.test_case
def run_test_vtctl_partial_clone_varbinary():
  _run_test_vtctl_partial_clone(create_vt_insert_test_varbinary,
                                populate_vt_insert_test_varbinary,
                                '00000000000000000000',
                                '03000000000000000000')

@utils.test_case
def run_test_restart_during_action():
  # Start up a master mysql and vttablet
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62344.start_vttablet()

  utils.run_vtctl('Ping ' + tablet_62344.zk_tablet_path)

  # schedule long action
  utils.run_vtctl('-no-wait Sleep %s 15s' % tablet_62344.zk_tablet_path, stdout=devnull)
  # ping blocks until the sleep finishes unless we have a schedule race
  action_path, _ = utils.run_vtctl('-no-wait Ping ' + tablet_62344.zk_tablet_path, trap_output=True)

  # kill agent leaving vtaction running
  tablet_62344.kill_vttablet()

  # restart agent
  tablet_62344.start_vttablet()

  # we expect this action with a short wait time to fail. this isn't the best
  # and has some potential for flakiness.
  utils.run_fail(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=WARNING -wait-time 2s WaitForAction ' + action_path)
  tablet_62344.kill_vttablet()


@utils.test_case
def run_test_reparent_down_master():
  utils.zk_wipe()

  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as they are serving
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62044.create_db('vt_test_keyspace')
  tablet_41983.create_db('vt_test_keyspace')
  tablet_31981.create_db('vt_test_keyspace')

  # Start up a master mysql and vttablet
  tablet_62344.init_tablet('master', 'test_keyspace', '0', start=True)

  # Create a few slaves for testing reparenting.
  tablet_62044.init_tablet('replica', 'test_keyspace', '0', start=True)
  tablet_41983.init_tablet('replica', 'test_keyspace', '0', start=True)
  tablet_31981.init_tablet('replica', 'test_keyspace', '0', start=True)

  # Recompute the shard layout node - until you do that, it might not be valid.
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.zk_check()

  # Force the slaves to reparent assuming that all the datasets are identical.
  utils.run_vtctl('ReparentShard -force /zk/global/vt/keyspaces/test_keyspace/shards/0 ' + tablet_62344.zk_tablet_path)
  utils.zk_check()

  # Make the master agent unavailable.
  tablet_62344.kill_vttablet()


  expected_addr = hostname + ':6700'
  _check_db_addr('test_keyspace.0.master:_vtocc', expected_addr)

  # Perform a reparent operation - this will hang for some amount of time.
  utils.run_fail(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=WARNING -wait-time 5s ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/0 ' + tablet_62044.zk_tablet_alias)

  # Should timeout and fail
  utils.run_fail(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=WARNING -wait-time 5s ScrapTablet ' + tablet_62344.zk_tablet_alias)

  # Force the scrap action in zk even though tablet is not accessible.
  utils.run_vtctl('ScrapTablet -force -skip-rebuild ' + tablet_62344.zk_tablet_path, auto_log=True)

  utils.run_fail(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=WARNING ChangeSlaveType -force %s idle' %
                 tablet_62344.zk_tablet_path)

  # Remove pending locks (make this the force option to ReparentShard?)
  utils.run_vtctl('PurgeActions /zk/global/vt/keyspaces/test_keyspace/shards/0/action')

  # Scrapping a tablet with -skip-rebuild shouldn't take it out of the
  # serving graph.
  expected_addr = hostname + ':6700'
  _check_db_addr('test_keyspace.0.master:_vtocc', expected_addr)

  # Re-run reparent operation, this shoud now proceed unimpeded.
  utils.run_vtctl('ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/0 ' + tablet_62044.zk_tablet_path)

  utils.zk_check()
  expected_addr = hostname + ':6701'
  _check_db_addr('test_keyspace.0.master:_vtocc', expected_addr)

  utils.run_vtctl('ChangeSlaveType -force %s idle' % tablet_62344.zk_tablet_path)

  idle_tablets, _ = utils.run_vtctl('ListIdle /zk/test_nj/vt', trap_output=True)
  if '0000062344' not in idle_tablets:
    raise utils.TestError('idle tablet not found', idle_tablets)

  tablet_62044.kill_vttablet()
  tablet_41983.kill_vttablet()
  tablet_31981.kill_vttablet()


@utils.test_case
def run_test_reparent_graceful_range_based():
  shard_id = '0000000000000000-FFFFFFFFFFFFFFFF'
  _run_test_reparent_graceful(shard_id)

@utils.test_case
def run_test_reparent_graceful():
  shard_id = '0'
  _run_test_reparent_graceful(shard_id)

def _run_test_reparent_graceful(shard_id):
  utils.zk_wipe()

  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as they are serving
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62044.create_db('vt_test_keyspace')
  tablet_41983.create_db('vt_test_keyspace')
  tablet_31981.create_db('vt_test_keyspace')

  # Start up a master mysql and vttablet
  tablet_62344.init_tablet('master', 'test_keyspace', shard_id, start=True)

  # Create a few slaves for testing reparenting.
  tablet_62044.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_41983.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_31981.init_tablet('replica', 'test_keyspace', shard_id, start=True)

  # Recompute the shard layout node - until you do that, it might not be valid.
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/' + shard_id)
  utils.zk_check()

  # Force the slaves to reparent assuming that all the datasets are identical.
  utils.pause("force ReparentShard?")
  utils.run_vtctl('ReparentShard -force /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62344.zk_tablet_path))
  utils.zk_check(ping_tablets=True)

  expected_addr = hostname + ':6700'
  _check_db_addr('test_keyspace.%s.master:_vtocc' % shard_id, expected_addr)

  # Convert two replica to spare. That should leave only one node serving traffic,
  # but still needs to appear in the replication graph.
  utils.run_vtctl('ChangeSlaveType ' + tablet_41983.zk_tablet_path + ' spare')
  utils.run_vtctl('ChangeSlaveType ' + tablet_31981.zk_tablet_path + ' spare')
  utils.zk_check()
  expected_addr = hostname + ':6701'
  _check_db_addr('test_keyspace.%s.replica:_vtocc' % shard_id, expected_addr)

  # Perform a graceful reparent operation.
  utils.pause("graceful ReparentShard?")
  utils.run_vtctl('ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62044.zk_tablet_path))
  utils.zk_check()

  expected_addr = hostname + ':6701'
  _check_db_addr('test_keyspace.%s.master:_vtocc' % shard_id, expected_addr)

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()
  tablet_41983.kill_vttablet()
  tablet_31981.kill_vttablet()

  # Test address correction.
  tablet_62044.start_vttablet(port=6773)
  # Wait a moment for address to reregister.
  time.sleep(1.0)

  expected_addr = hostname + ':6773'
  _check_db_addr('test_keyspace.%s.master:_vtocc' % shard_id, expected_addr)

  tablet_62044.kill_vttablet()


# This is a manual test to check error formatting.
@utils.test_case
def run_test_reparent_slave_offline(shard_id='0'):
  utils.zk_wipe()

  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as they are serving
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62044.create_db('vt_test_keyspace')
  tablet_41983.create_db('vt_test_keyspace')
  tablet_31981.create_db('vt_test_keyspace')

  # Start up a master mysql and vttablet
  tablet_62344.init_tablet('master', 'test_keyspace', shard_id, start=True)

  # Create a few slaves for testing reparenting.
  tablet_62044.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_41983.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_31981.init_tablet('replica', 'test_keyspace', shard_id, start=True)

  # Recompute the shard layout node - until you do that, it might not be valid.
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/' + shard_id)
  utils.zk_check()

  # Force the slaves to reparent assuming that all the datasets are identical.
  utils.run_vtctl('ReparentShard -force /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62344.zk_tablet_path))
  utils.zk_check(ping_tablets=True)

  expected_addr = hostname + ':6700'
  _check_db_addr('test_keyspace.%s.master:_vtocc' % shard_id, expected_addr)

  # Kill one tablet so we seem offline
  tablet_31981.kill_vttablet()

  # Perform a graceful reparent operation.
  utils.run_vtctl('ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62044.zk_tablet_path))

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()
  tablet_41983.kill_vttablet()


# See if a lag slave can be safely reparent.
@utils.test_case
def run_test_reparent_lag_slave(shard_id='0'):
  utils.zk_wipe()

  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as they are serving
  tablet_62344.create_db('vt_test_keyspace')
  tablet_62044.create_db('vt_test_keyspace')
  tablet_41983.create_db('vt_test_keyspace')
  tablet_31981.create_db('vt_test_keyspace')

  # Start up a master mysql and vttablet
  tablet_62344.init_tablet('master', 'test_keyspace', shard_id, start=True)

  # Create a few slaves for testing reparenting.
  tablet_62044.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_31981.init_tablet('replica', 'test_keyspace', shard_id, start=True)
  tablet_41983.init_tablet('lag', 'test_keyspace', shard_id, start=True)

  # Recompute the shard layout node - until you do that, it might not be valid.
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/' + shard_id)
  utils.zk_check()

  # Force the slaves to reparent assuming that all the datasets are identical.
  utils.run_vtctl('ReparentShard -force /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62344.zk_tablet_path))
  utils.zk_check(ping_tablets=True)

  tablet_62344.create_db('vt_test_keyspace')
  tablet_62344.mquery('vt_test_keyspace', create_vt_insert_test)

  tablet_41983.mquery('', 'stop slave')
  for q in populate_vt_insert_test:
    tablet_62344.mquery('vt_test_keyspace', q, write=True)

  # Perform a graceful reparent operation.
  utils.run_vtctl('ReparentShard /zk/global/vt/keyspaces/test_keyspace/shards/%s %s' % (shard_id, tablet_62044.zk_tablet_path))

  tablet_41983.mquery('', 'start slave')
  time.sleep(1)

  utils.pause("check orphan")

  utils.run_vtctl('ReparentTablet %s' % tablet_41983.zk_tablet_path)

  result = tablet_41983.mquery('vt_test_keyspace', 'select msg from vt_insert_test where id=1')
  if len(result) != 1:
    raise utils.TestError('expected 1 row from vt_insert_test', result)

  utils.pause("check lag reparent")

  tablet_62344.kill_vttablet()
  tablet_62044.kill_vttablet()
  tablet_41983.kill_vttablet()
  tablet_31981.kill_vttablet()



@utils.test_case
def run_test_vttablet_authenticated():
  utils.zk_wipe()
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')
  tablet_62344.init_tablet('master', 'test_keyspace', '0')
  utils.run_vtctl('RebuildShardGraph /zk/global/vt/keyspaces/test_keyspace/shards/0')
  utils.run_vtctl('Validate /zk/global/vt/keyspaces')

  tablet_62344.populate('vt_test_keyspace', create_vt_select_test,
                        populate_vt_select_test)
  agent = tablet_62344.start_vttablet(auth=True)
  utils.run_vtctl('SetReadWrite ' + tablet_62344.zk_tablet_path)

  err, out = tablet_62344.vquery('select * from vt_select_test', dbname='vt_test_keyspace', user='ala', password=r'ma kota')
  if 'Row count: ' not in out:
    raise utils.TestError("query didn't go through: %s, %s" % (err, out))

  utils.kill_sub_process(agent)
  # TODO(szopa): Test that non-authenticated queries do not pass
  # through (when we get to that point).

def _run_hook(params, expectedStrings):
  out, err = utils.run(vtroot+'/bin/vtctl -logfile=/dev/null -log.level=INFO ExecuteHook %s %s' % (tablet_62344.zk_tablet_path, params), trap_output=True, raise_on_error=False)
  for expected in expectedStrings:
    if err.find(expected) == -1:
      print "ExecuteHook output:"
      print err
      raise utils.TestError('ExecuteHook returned unexpected result, no string: ' + expected)


@utils.test_case
def run_test_hook():
  utils.zk_wipe()
  utils.run_vtctl('CreateKeyspace -force /zk/global/vt/keyspaces/test_keyspace')

  # create the database so vttablets start, as it is serving
  tablet_62344.create_db('vt_test_keyspace')

  tablet_62344.init_tablet('master', 'test_keyspace', '0', start=True)

  # test a regular program works
  _run_hook("test.sh flag1 param1=hello", [
      '"ExitStatus": 0',
      '"Stdout": "PARAM: --flag1\\nPARAM: --param1=hello\\n"',
      '"Stderr": ""',
      ])

  # test stderr output
  _run_hook("test.sh to-stderr", [
      '"ExitStatus": 0',
      '"Stdout": "PARAM: --to-stderr\\n"',
      '"Stderr": "ERR: --to-stderr\\n"',
      ])

  # test commands that fail
  _run_hook("test.sh exit-error", [
      '"ExitStatus": 1',
      '"Stdout": "PARAM: --exit-error\\n"',
      '"Stderr": "ERROR: exit status 1\\n"',
      ])

  # test hook that is not present
  _run_hook("not_here.sh", [
      '"ExitStatus": -1',
      '"Stdout": "Skipping missing hook: /', # cannot go further, local path
      '"Stderr": ""',
      ])

  # test hook with invalid name
  _run_hook("/bin/ls", [
      "FATAL: action failed: ExecuteHook hook name cannot have a '/' in it",
      ])

  tablet_62344.kill_vttablet()


def run_all():
  run_test_sanity()
  run_test_sanity() # run twice to check behavior with existing znode data
  run_test_scrap()
  run_test_restart_during_action()

  # Subsumed by vtctl_clone test.
  # run_test_mysqlctl_clone()
  run_test_vtctl_clone()

  # This test does not pass as it requires an experimental mysql patch.
  #run_test_vtctl_partial_clone()
  #run_test_vtctl_partial_clone_varbinary()

  run_test_reparent_graceful()
  run_test_reparent_graceful_range_based()
  run_test_reparent_down_master()
  run_test_vttablet_authenticated()
  run_test_reparent_lag_slave()

def main():
  parser = OptionParser()
  parser.add_option('-v', '--verbose', action='store_true')
  parser.add_option('-d', '--debug', action='store_true')
  parser.add_option('--skip-teardown', action='store_true')
  (utils.options, args) = parser.parse_args()

  if not args:
    args = ['run_all']

  try:
    if args[0] != 'teardown':
      setup()
      if args[0] != 'setup':
        for arg in args:
          globals()[arg]()
          print "GREAT SUCCESS"
  except KeyboardInterrupt:
    pass
  except utils.Break:
    utils.options.skip_teardown = True
  finally:
    teardown()


if __name__ == '__main__':
  main()
