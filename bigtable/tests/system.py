# Copyright 2016 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import operator
import os

import unittest

from google.cloud._helpers import _datetime_from_microseconds
from google.cloud._helpers import _microseconds_from_datetime
from google.cloud._helpers import UTC
from google.cloud.bigtable.client import Client
from google.cloud.bigtable.column_family import MaxVersionsGCRule
from google.cloud.bigtable.row_filters import ApplyLabelFilter
from google.cloud.bigtable.row_filters import ColumnQualifierRegexFilter
from google.cloud.bigtable.row_filters import RowFilterChain
from google.cloud.bigtable.row_filters import RowFilterUnion
from google.cloud.bigtable.row_data import Cell
from google.cloud.bigtable.row_data import PartialRowData
from google.cloud.environment_vars import BIGTABLE_EMULATOR
from google.cloud.bigtable.row_set import RowSet
from google.cloud.bigtable.row_set import RowRange

from test_utils.retry import RetryErrors
from test_utils.system import EmulatorCreds
from test_utils.system import unique_resource_id

LOCATION_ID = 'us-central1-c'
INSTANCE_ID = 'g-c-p' + unique_resource_id('-')
LABELS = {u'foo': u'bar'}
TABLE_ID = 'google-cloud-python-test-table'
CLUSTER_ID = INSTANCE_ID+'-cluster'
COLUMN_FAMILY_ID1 = u'col-fam-id1'
COLUMN_FAMILY_ID2 = u'col-fam-id2'
COL_NAME1 = b'col-name1'
COL_NAME2 = b'col-name2'
COL_NAME3 = b'col-name3-but-other-fam'
CELL_VAL1 = b'cell-val'
CELL_VAL2 = b'cell-val-newer'
CELL_VAL3 = b'altcol-cell-val'
CELL_VAL4 = b'foo'
ROW_KEY = b'row-key'
ROW_KEY_ALT = b'row-key-alt'
ROUTING_POLICY_TYPE_ANY = 1
ROUTING_POLICY_TYPE_SINGLE = 2
EXISTING_INSTANCES = []


class Config(object):
    """Run-time configuration to be modified at set-up.

    This is a mutable stand-in to allow test set-up to modify
    global state.
    """
    CLIENT = None
    INSTANCE = None
    IN_EMULATOR = False


def _retry_on_unavailable(exc):
    """Retry only errors whose status code is 'UNAVAILABLE'."""
    from grpc import StatusCode
    return exc.code() == StatusCode.UNAVAILABLE


def setUpModule():
    from google.cloud.exceptions import GrpcRendezvous

    Config.IN_EMULATOR = os.getenv(BIGTABLE_EMULATOR) is not None

    if Config.IN_EMULATOR:
        credentials = EmulatorCreds()
        Config.CLIENT = Client(admin=True, credentials=credentials)
    else:
        Config.CLIENT = Client(admin=True)

    Config.INSTANCE = Config.CLIENT.instance(INSTANCE_ID, labels=LABELS)

    if not Config.IN_EMULATOR:
        retry = RetryErrors(GrpcRendezvous,
                            error_predicate=_retry_on_unavailable)
        instances, failed_locations = retry(Config.CLIENT.list_instances)()

        if len(failed_locations) != 0:
            raise ValueError('List instances failed in module set up.')

        EXISTING_INSTANCES[:] = instances

        # After listing, create the test instance.
        created_op = Config.INSTANCE.create(location_id=LOCATION_ID)
        created_op.result(timeout=10)


def tearDownModule():
    if not Config.IN_EMULATOR:
        Config.INSTANCE.delete()


class TestInstanceAdminAPI(unittest.TestCase):

    def setUp(self):
        if Config.IN_EMULATOR:
            self.skipTest(
                'Instance Admin API not supported in Bigtable emulator')
        self.instances_to_delete = []

    def tearDown(self):
        for instance in self.instances_to_delete:
            instance.delete()

    def test_list_instances(self):
        instances, failed_locations = Config.CLIENT.list_instances()

        self.assertEqual(failed_locations, [])
        found = set([instance.name for instance in instances])
        self.assertTrue(Config.INSTANCE.name in found)

    def test_reload(self):
        from google.cloud.bigtable import enums
        # Use same arguments as Config.INSTANCE (created in `setUpModule`)
        # so we can use reload() on a fresh instance.
        instance = Config.CLIENT.instance(INSTANCE_ID)
        # Make sure metadata unset before reloading.
        instance.display_name = None

        instance.reload()
        self.assertEqual(instance.display_name, Config.INSTANCE.display_name)
        self.assertEqual(instance.labels, Config.INSTANCE.labels)
        self.assertEqual(instance.type_, enums.InstanceType.PRODUCTION)

    def test_create_instance_defaults(self):
        from google.cloud.bigtable import enums

        ALT_INSTANCE_ID = 'ndef' + unique_resource_id('-')
        instance = Config.CLIENT.instance(ALT_INSTANCE_ID)
        operation = instance.create(location_id=LOCATION_ID)
        # Make sure this instance gets deleted after the test case.
        self.instances_to_delete.append(instance)

        # We want to make sure the operation completes.
        operation.result(timeout=10)

        # Create a new instance instance and make sure it is the same.
        instance_alt = Config.CLIENT.instance(ALT_INSTANCE_ID)
        instance_alt.reload()

        self.assertEqual(instance, instance_alt)
        self.assertEqual(instance.display_name, instance_alt.display_name)
        # Make sure that by default a PRODUCTION type instance is created
        self.assertIsNone(instance.type_)
        self.assertEqual(instance_alt.type_, enums.InstanceType.PRODUCTION)
        self.assertIsNone(instance.labels)
        self.assertFalse(instance_alt.labels)

    def test_create_instance(self):
        from google.cloud.bigtable import enums
        _DEVELOPMENT = enums.InstanceType.DEVELOPMENT

        ALT_INSTANCE_ID = 'new' + unique_resource_id('-')
        instance = Config.CLIENT.instance(ALT_INSTANCE_ID,
                                          instance_type=_DEVELOPMENT,
                                          labels=LABELS)
        operation = instance.create(location_id=LOCATION_ID, serve_nodes=None)
        # Make sure this instance gets deleted after the test case.
        self.instances_to_delete.append(instance)

        # We want to make sure the operation completes.
        operation.result(timeout=10)

        # Create a new instance instance and make sure it is the same.
        instance_alt = Config.CLIENT.instance(ALT_INSTANCE_ID)
        instance_alt.reload()

        self.assertEqual(instance, instance_alt)
        self.assertEqual(instance.display_name, instance_alt.display_name)
        self.assertEqual(instance.type_, instance_alt.type_)
        self.assertEqual(instance.labels, instance_alt.labels)

    def test_update_display_name_and_labels(self):
        OLD_DISPLAY_NAME = Config.INSTANCE.display_name
        NEW_DISPLAY_NAME = 'Foo Bar Baz'
        NEW_LABELS = {'foo_bar': 'foo_bar'}
        Config.INSTANCE.display_name = NEW_DISPLAY_NAME
        Config.INSTANCE.labels = NEW_LABELS
        operation = Config.INSTANCE.update()

        # We want to make sure the operation completes.
        operation.result(timeout=10)

        # Create a new instance instance and reload it.
        instance_alt = Config.CLIENT.instance(INSTANCE_ID, labels=LABELS)
        self.assertEqual(instance_alt.display_name, OLD_DISPLAY_NAME)
        self.assertEqual(instance_alt.labels, LABELS)
        instance_alt.reload()
        self.assertEqual(instance_alt.display_name, NEW_DISPLAY_NAME)
        self.assertEqual(instance_alt.labels, NEW_LABELS)

        # Make sure to put the instance back the way it was for the
        # other test cases.
        Config.INSTANCE.display_name = OLD_DISPLAY_NAME
        Config.INSTANCE.labels = LABELS
        operation = Config.INSTANCE.update()

        # We want to make sure the operation completes.
        operation.result(timeout=10)

    def test_update_type(self):
        from google.cloud.bigtable.enums import InstanceType

        _DEVELOPMENT = InstanceType.DEVELOPMENT
        _PRODUCTION = InstanceType.PRODUCTION
        ALT_INSTANCE_ID = 'new' + unique_resource_id('-')
        instance = Config.CLIENT.instance(ALT_INSTANCE_ID,
                                          instance_type=_DEVELOPMENT)
        operation = instance.create(location_id=LOCATION_ID, serve_nodes=None)
        # Make sure this instance gets deleted after the test case.
        self.instances_to_delete.append(instance)

        # We want to make sure the operation completes.
        operation.result(timeout=10)

        # Unset the display_name
        instance.display_name = None

        instance.type_ = _PRODUCTION
        operation = instance.update()

        # We want to make sure the operation completes.
        operation.result(timeout=10)

        # Create a new instance instance and reload it.
        instance_alt = Config.CLIENT.instance(ALT_INSTANCE_ID)
        self.assertIsNone(instance_alt.type_)
        instance_alt.reload()
        self.assertEqual(instance_alt.type_, _PRODUCTION)

    def test_create_app_profile_with_multi_routing_policy(self):
        from google.cloud.bigtable_admin_v2.types import instance_pb2

        description = 'Foo App Profile'
        instance = Config.INSTANCE
        ignore_warnings = True
        app_profile_id = 'app_profile_id_1'

        app_profile = instance.create_app_profile(
            app_profile_id=app_profile_id,
            routing_policy_type=ROUTING_POLICY_TYPE_ANY,
            description=description,
            ignore_warnings=ignore_warnings
        )

        # Load a different app_profile objec form the server and
        # verrify that it is the same
        alt_app_profile = instance.get_app_profile(app_profile_id)
        self.assertEqual(app_profile, alt_app_profile)

        # Modify existing app_profile to singly routing policy and confirm
        new_description = 'To single routing policy'
        allow_transactional_writes = False
        operation = instance.update_app_profile(
            app_profile_id=app_profile_id,
            routing_policy_type=ROUTING_POLICY_TYPE_SINGLE,
            description=new_description,
            cluster_id=CLUSTER_ID,
            allow_transactional_writes=allow_transactional_writes)
        operation.result(timeout=10)

        alt_app_profile = instance.get_app_profile(app_profile_id)
        self.assertEqual(alt_app_profile.description, new_description)
        self.assertIsInstance(
            alt_app_profile.single_cluster_routing,
            instance_pb2.AppProfile.SingleClusterRouting)
        self.assertEqual(
            alt_app_profile.single_cluster_routing.cluster_id, CLUSTER_ID)
        self.assertEqual(
            alt_app_profile.single_cluster_routing.allow_transactional_writes,
            allow_transactional_writes)

        # Delete app_profile
        instance.delete_app_profile(app_profile_id=app_profile_id,
                                    ignore_warnings=ignore_warnings)
        self.assertFalse(self._app_profile_exists(app_profile_id))

    def test_create_app_profile_with_single_routing_policy(self):
        from google.cloud.bigtable_admin_v2.types import instance_pb2

        description = 'Foo App Profile'
        instance = Config.INSTANCE
        ignore_warnings = True
        app_profile_id = 'app_profile_id_2'

        app_profile = instance.create_app_profile(
            app_profile_id=app_profile_id,
            routing_policy_type=ROUTING_POLICY_TYPE_SINGLE,
            description=description,
            cluster_id=CLUSTER_ID,
        )

        # Load a different app_profile objec form the server and
        # verrify that it is the same
        alt_app_profile = instance.get_app_profile(app_profile_id)
        self.assertEqual(app_profile, alt_app_profile)

        # Modify existing app_profile to allow_transactional_writes
        new_description = 'Allow transactional writes'
        allow_transactional_writes = True
        # Note: Do not need to ignore warnings when switching
        # to allow transactional writes.
        # Do need to set ignore_warnings to True, when switching to
        # disallow the transactional writes.
        operation = instance.update_app_profile(
            app_profile_id=app_profile_id,
            routing_policy_type=ROUTING_POLICY_TYPE_SINGLE,
            description=new_description,
            cluster_id=CLUSTER_ID,
            allow_transactional_writes=allow_transactional_writes)
        operation.result(timeout=10)

        alt_app_profile = instance.get_app_profile(app_profile_id)
        self.assertEqual(alt_app_profile.description, new_description)
        self.assertEqual(
            alt_app_profile.single_cluster_routing.allow_transactional_writes,
            allow_transactional_writes)

        # Modify existing app_proflie to multi cluster routing
        new_description = 'To multi cluster routing'
        operation = instance.update_app_profile(
            app_profile_id=app_profile_id,
            routing_policy_type=ROUTING_POLICY_TYPE_ANY,
            description=new_description,
            ignore_warnings=ignore_warnings)
        operation.result(timeout=10)

        alt_app_profile = instance.get_app_profile(app_profile_id)
        self.assertEqual(alt_app_profile.description, new_description)
        self.assertIsInstance(
            alt_app_profile.multi_cluster_routing_use_any,
            instance_pb2.AppProfile.MultiClusterRoutingUseAny)

    def _app_profile_exists(self, app_profile_id):
        from google.api_core import exceptions
        try:
            Config.INSTANCE.get_app_profile(app_profile_id)
        except exceptions.NotFound:
            return False
        else:
            return True


class TestTableAdminAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._table = Config.INSTANCE.table(TABLE_ID)
        cls._table.create()

    @classmethod
    def tearDownClass(cls):
        cls._table.delete()

    def setUp(self):
        self.tables_to_delete = []

    def tearDown(self):
        for table in self.tables_to_delete:
            table.delete()

    def test_list_tables(self):
        # Since `Config.INSTANCE` is newly created in `setUpModule`, the table
        # created in `setUpClass` here will be the only one.
        tables = Config.INSTANCE.list_tables()
        self.assertEqual(tables, [self._table])

    def test_create_table(self):
        temp_table_id = 'test-create-table'
        temp_table = Config.INSTANCE.table(temp_table_id)
        temp_table.create()
        self.tables_to_delete.append(temp_table)

        # First, create a sorted version of our expected result.
        name_attr = operator.attrgetter('name')
        expected_tables = sorted([temp_table, self._table], key=name_attr)

        # Then query for the tables in the instance and sort them by
        # name as well.
        tables = Config.INSTANCE.list_tables()
        sorted_tables = sorted(tables, key=name_attr)
        self.assertEqual(sorted_tables, expected_tables)

    def test_create_table_with_families(self):
        temp_table_id = 'test-create-table-with-failies'
        temp_table = Config.INSTANCE.table(temp_table_id)
        gc_rule = MaxVersionsGCRule(1)
        temp_table.create(column_families={COLUMN_FAMILY_ID1: gc_rule})
        self.tables_to_delete.append(temp_table)

        col_fams = temp_table.list_column_families()

        self.assertEqual(len(col_fams), 1)
        retrieved_col_fam = col_fams[COLUMN_FAMILY_ID1]
        self.assertIs(retrieved_col_fam._table, temp_table)
        self.assertEqual(retrieved_col_fam.column_family_id,
                         COLUMN_FAMILY_ID1)
        self.assertEqual(retrieved_col_fam.gc_rule, gc_rule)

    def test_create_table_with_split_keys(self):
        temp_table_id = 'foo-bar-baz-split-table'
        initial_split_keys = [b'split_key_1', b'split_key_10',
                              b'split_key_20']
        temp_table = Config.INSTANCE.table(temp_table_id)
        temp_table.create(initial_split_keys=initial_split_keys)
        self.tables_to_delete.append(temp_table)

        # Read Sample Row Keys for created splits
        sample_row_keys = temp_table.sample_row_keys()
        actual_keys = [srk.row_key for srk in sample_row_keys]

        expected_keys = initial_split_keys
        expected_keys.append(b'')

        self.assertEqual(actual_keys, expected_keys)

    def test_create_column_family(self):
        temp_table_id = 'test-create-column-family'
        temp_table = Config.INSTANCE.table(temp_table_id)
        temp_table.create()
        self.tables_to_delete.append(temp_table)

        self.assertEqual(temp_table.list_column_families(), {})
        gc_rule = MaxVersionsGCRule(1)
        column_family = temp_table.column_family(COLUMN_FAMILY_ID1,
                                                 gc_rule=gc_rule)
        column_family.create()

        col_fams = temp_table.list_column_families()

        self.assertEqual(len(col_fams), 1)
        retrieved_col_fam = col_fams[COLUMN_FAMILY_ID1]
        self.assertIs(retrieved_col_fam._table, column_family._table)
        self.assertEqual(retrieved_col_fam.column_family_id,
                         column_family.column_family_id)
        self.assertEqual(retrieved_col_fam.gc_rule, gc_rule)

    def test_update_column_family(self):
        temp_table_id = 'test-update-column-family'
        temp_table = Config.INSTANCE.table(temp_table_id)
        temp_table.create()
        self.tables_to_delete.append(temp_table)

        gc_rule = MaxVersionsGCRule(1)
        column_family = temp_table.column_family(COLUMN_FAMILY_ID1,
                                                 gc_rule=gc_rule)
        column_family.create()

        # Check that our created table is as expected.
        col_fams = temp_table.list_column_families()
        self.assertEqual(col_fams, {COLUMN_FAMILY_ID1: column_family})

        # Update the column family's GC rule and then try to update.
        column_family.gc_rule = None
        column_family.update()

        # Check that the update has propagated.
        col_fams = temp_table.list_column_families()
        self.assertIsNone(col_fams[COLUMN_FAMILY_ID1].gc_rule)

    def test_delete_column_family(self):
        temp_table_id = 'test-delete-column-family'
        temp_table = Config.INSTANCE.table(temp_table_id)
        temp_table.create()
        self.tables_to_delete.append(temp_table)

        self.assertEqual(temp_table.list_column_families(), {})
        column_family = temp_table.column_family(COLUMN_FAMILY_ID1)
        column_family.create()

        # Make sure the family is there before deleting it.
        col_fams = temp_table.list_column_families()
        self.assertEqual(list(col_fams.keys()), [COLUMN_FAMILY_ID1])

        column_family.delete()
        # Make sure we have successfully deleted it.
        self.assertEqual(temp_table.list_column_families(), {})


class TestDataAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._table = table = Config.INSTANCE.table('test-data-api')
        table.create()
        table.column_family(COLUMN_FAMILY_ID1).create()
        table.column_family(COLUMN_FAMILY_ID2).create()

    @classmethod
    def tearDownClass(cls):
        # Will also delete any data contained in the table.
        cls._table.delete()

    def _maybe_emulator_skip(self, message):
        # NOTE: This method is necessary because ``Config.IN_EMULATOR``
        #       is set at runtime rather than import time, which means we
        #       can't use the @unittest.skipIf decorator.
        if Config.IN_EMULATOR:
            self.skipTest(message)

    def setUp(self):
        self.rows_to_delete = []

    def tearDown(self):
        for row in self.rows_to_delete:
            row.clear()
            row.delete()
            row.commit()

    def _write_to_row(self, row1=None, row2=None, row3=None, row4=None):
        timestamp1 = datetime.datetime.utcnow().replace(tzinfo=UTC)
        timestamp1_micros = _microseconds_from_datetime(timestamp1)
        # Truncate to millisecond granularity.
        timestamp1_micros -= (timestamp1_micros % 1000)
        timestamp1 = _datetime_from_microseconds(timestamp1_micros)
        # 1000 microseconds is a millisecond
        timestamp2 = timestamp1 + datetime.timedelta(microseconds=1000)
        timestamp2_micros = _microseconds_from_datetime(timestamp2)
        timestamp3 = timestamp1 + datetime.timedelta(microseconds=2000)
        timestamp3_micros = _microseconds_from_datetime(timestamp3)
        timestamp4 = timestamp1 + datetime.timedelta(microseconds=3000)
        timestamp4_micros = _microseconds_from_datetime(timestamp4)

        if row1 is not None:
            row1.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, CELL_VAL1,
                          timestamp=timestamp1)
        if row2 is not None:
            row2.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, CELL_VAL2,
                          timestamp=timestamp2)
        if row3 is not None:
            row3.set_cell(COLUMN_FAMILY_ID1, COL_NAME2, CELL_VAL3,
                          timestamp=timestamp3)
        if row4 is not None:
            row4.set_cell(COLUMN_FAMILY_ID2, COL_NAME3, CELL_VAL4,
                          timestamp=timestamp4)

        # Create the cells we will check.
        cell1 = Cell(CELL_VAL1, timestamp1_micros)
        cell2 = Cell(CELL_VAL2, timestamp2_micros)
        cell3 = Cell(CELL_VAL3, timestamp3_micros)
        cell4 = Cell(CELL_VAL4, timestamp4_micros)
        return cell1, cell2, cell3, cell4

    def test_timestamp_filter_millisecond_granularity(self):
        from google.cloud.bigtable import row_filters

        end = datetime.datetime.now()
        start = end - datetime.timedelta(minutes=60)
        timestamp_range = row_filters.TimestampRange(start=start, end=end)
        timefilter = row_filters.TimestampRangeFilter(timestamp_range)
        row_data = self._table.read_rows(filter_=timefilter)
        row_data.consume_all()

    def test_mutate_rows(self):
        row1 = self._table.row(ROW_KEY)
        row1.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, CELL_VAL1)
        row1.commit()
        self.rows_to_delete.append(row1)
        row2 = self._table.row(ROW_KEY_ALT)
        row2.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, CELL_VAL2)
        row2.commit()
        self.rows_to_delete.append(row2)

        # Change the contents
        row1.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, CELL_VAL3)
        row2.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, CELL_VAL4)
        rows = [row1, row2]
        statuses = self._table.mutate_rows(rows)
        result = [status.code for status in statuses]
        expected_result = [0, 0]
        self.assertEqual(result, expected_result)

        # Check the contents
        row1_data = self._table.read_row(ROW_KEY)
        self.assertEqual(
            row1_data.cells[COLUMN_FAMILY_ID1][COL_NAME1][0].value, CELL_VAL3)
        row2_data = self._table.read_row(ROW_KEY_ALT)
        self.assertEqual(
            row2_data.cells[COLUMN_FAMILY_ID1][COL_NAME1][0].value, CELL_VAL4)

    def test_truncate_table(self):
        row_keys = [
            b'row_key_1', b'row_key_2', b'row_key_3', b'row_key_4',
            b'row_key_5', b'row_key_pr_1', b'row_key_pr_2', b'row_key_pr_3',
            b'row_key_pr_4', b'row_key_pr_5']

        for row_key in row_keys:
            row = self._table.row(row_key)
            row.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, CELL_VAL1)
            row.commit()
            self.rows_to_delete.append(row)

        self._table.truncate(timeout=200)

        read_rows = self._table.yield_rows()

        for row in read_rows:
            self.assertNotIn(row.row_key.decode('utf-8'), row_keys)

    def test_drop_by_prefix_table(self):
        row_keys = [
            b'row_key_1', b'row_key_2', b'row_key_3', b'row_key_4',
            b'row_key_5', b'row_key_pr_1', b'row_key_pr_2', b'row_key_pr_3',
            b'row_key_pr_4', b'row_key_pr_5']

        for row_key in row_keys:
            row = self._table.row(row_key)
            row.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, CELL_VAL1)
            row.commit()
            self.rows_to_delete.append(row)

        self._table.drop_by_prefix(row_key_prefix='row_key_pr', timeout=200)

        read_rows = self._table.yield_rows()
        expected_rows_count = 5
        read_rows_count = 0

        for row in read_rows:
            if row.row_key in row_keys:
                read_rows_count += 1

        self.assertEqual(expected_rows_count, read_rows_count)

    def test_yield_rows_with_row_set(self):
        row_keys = [
            b'row_key_1', b'row_key_2', b'row_key_3', b'row_key_4',
            b'row_key_5', b'row_key_6', b'row_key_7', b'row_key_8',
            b'row_key_9']

        rows = []
        for row_key in row_keys:
            row = self._table.row(row_key)
            row.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, CELL_VAL1)
            rows.append(row)
            self.rows_to_delete.append(row)
        self._table.mutate_rows(rows)

        row_set = RowSet()
        row_set.add_row_range(RowRange(start_key=b'row_key_3',
                                       end_key=b'row_key_7'))
        row_set.add_row_key(b'row_key_1')

        read_rows = self._table.yield_rows(row_set=row_set)

        expected_row_keys = set([b'row_key_1', b'row_key_3', b'row_key_4',
                                 b'row_key_5', b'row_key_6'])
        found_row_keys = set([row.row_key for row in read_rows])
        self.assertEqual(found_row_keys, set(expected_row_keys))

    def test_read_large_cell_limit(self):
        row = self._table.row(ROW_KEY)
        self.rows_to_delete.append(row)

        number_of_bytes = 10 * 1024 * 1024
        data = b'1' * number_of_bytes  # 10MB of 1's.
        row.set_cell(COLUMN_FAMILY_ID1, COL_NAME1, data)
        row.commit()

        # Read back the contents of the row.
        partial_row_data = self._table.read_row(ROW_KEY)
        self.assertEqual(partial_row_data.row_key, ROW_KEY)
        cell = partial_row_data.cells[COLUMN_FAMILY_ID1]
        column = cell[COL_NAME1]
        self.assertEqual(len(column), 1)
        self.assertEqual(column[0].value, data)

    def test_read_row(self):
        row = self._table.row(ROW_KEY)
        self.rows_to_delete.append(row)

        cell1, cell2, cell3, cell4 = self._write_to_row(row, row, row, row)
        row.commit()

        # Read back the contents of the row.
        partial_row_data = self._table.read_row(ROW_KEY)
        self.assertEqual(partial_row_data.row_key, ROW_KEY)

        # Check the cells match.
        ts_attr = operator.attrgetter('timestamp')
        expected_row_contents = {
            COLUMN_FAMILY_ID1: {
                COL_NAME1: sorted([cell1, cell2], key=ts_attr, reverse=True),
                COL_NAME2: [cell3],
            },
            COLUMN_FAMILY_ID2: {
                COL_NAME3: [cell4],
            },
        }
        self.assertEqual(partial_row_data.cells, expected_row_contents)

    def test_read_rows(self):
        row = self._table.row(ROW_KEY)
        row_alt = self._table.row(ROW_KEY_ALT)
        self.rows_to_delete.extend([row, row_alt])

        cell1, cell2, cell3, cell4 = self._write_to_row(row, row_alt,
                                                        row, row_alt)
        row.commit()
        row_alt.commit()

        rows_data = self._table.read_rows()
        self.assertEqual(rows_data.rows, {})
        rows_data.consume_all()

        # NOTE: We should refrain from editing protected data on instances.
        #       Instead we should make the values public or provide factories
        #       for constructing objects with them.
        row_data = PartialRowData(ROW_KEY)
        row_data._chunks_encountered = True
        row_data._committed = True
        row_data._cells = {
            COLUMN_FAMILY_ID1: {
                COL_NAME1: [cell1],
                COL_NAME2: [cell3],
            },
        }

        row_alt_data = PartialRowData(ROW_KEY_ALT)
        row_alt_data._chunks_encountered = True
        row_alt_data._committed = True
        row_alt_data._cells = {
            COLUMN_FAMILY_ID1: {
                COL_NAME1: [cell2],
            },
            COLUMN_FAMILY_ID2: {
                COL_NAME3: [cell4],
            },
        }

        expected_rows = {
            ROW_KEY: row_data,
            ROW_KEY_ALT: row_alt_data,
        }
        self.assertEqual(rows_data.rows, expected_rows)

    def test_read_with_label_applied(self):
        self._maybe_emulator_skip('Labels not supported by Bigtable emulator')
        row = self._table.row(ROW_KEY)
        self.rows_to_delete.append(row)

        cell1, _, cell3, _ = self._write_to_row(row, None, row)
        row.commit()

        # Combine a label with column 1.
        label1 = u'label-red'
        label1_filter = ApplyLabelFilter(label1)
        col1_filter = ColumnQualifierRegexFilter(COL_NAME1)
        chain1 = RowFilterChain(filters=[col1_filter, label1_filter])

        # Combine a label with column 2.
        label2 = u'label-blue'
        label2_filter = ApplyLabelFilter(label2)
        col2_filter = ColumnQualifierRegexFilter(COL_NAME2)
        chain2 = RowFilterChain(filters=[col2_filter, label2_filter])

        # Bring our two labeled columns together.
        row_filter = RowFilterUnion(filters=[chain1, chain2])
        partial_row_data = self._table.read_row(ROW_KEY, filter_=row_filter)
        self.assertEqual(partial_row_data.row_key, ROW_KEY)

        cells_returned = partial_row_data.cells
        col_fam1 = cells_returned.pop(COLUMN_FAMILY_ID1)
        # Make sure COLUMN_FAMILY_ID1 was the only key.
        self.assertEqual(len(cells_returned), 0)

        cell1_new, = col_fam1.pop(COL_NAME1)
        cell3_new, = col_fam1.pop(COL_NAME2)
        # Make sure COL_NAME1 and COL_NAME2 were the only keys.
        self.assertEqual(len(col_fam1), 0)

        # Check that cell1 has matching values and gained a label.
        self.assertEqual(cell1_new.value, cell1.value)
        self.assertEqual(cell1_new.timestamp, cell1.timestamp)
        self.assertEqual(cell1.labels, [])
        self.assertEqual(cell1_new.labels, [label1])

        # Check that cell3 has matching values and gained a label.
        self.assertEqual(cell3_new.value, cell3.value)
        self.assertEqual(cell3_new.timestamp, cell3.timestamp)
        self.assertEqual(cell3.labels, [])
        self.assertEqual(cell3_new.labels, [label2])
