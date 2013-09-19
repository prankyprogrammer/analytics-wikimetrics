import datetime
import decimal
from nose.tools import assert_true, assert_equals
from unittest import TestCase
from wikimetrics.utils import (
    stringify,
)
from wikimetrics.metrics import NamespaceEdits


class UtilsTest(TestCase):
    
    def test_better_encoder_date(self):
        result = stringify(date_not_date_time=datetime.date(2013, 06, 01))
        print result
        assert_true(result.find('"date_not_date_time"') >= 0)
        assert_true(result.find('2013-06-01') >= 0)
    
    def test_better_encoder_datetime(self):
        result = stringify(date_time=datetime.datetime(2013, 06, 01, 02, 03, 04))
        print result
        assert_true(result.find('"date_time"') >= 0)
        assert_true(result.find('2013-06-01 02:03:04') >= 0)
    
    def test_better_encoder_decimal(self):
        result = stringify(deci=decimal.Decimal(6.01))
        assert_true(result.find('"deci"') >= 0)
        assert_true(result.find('6.01') >= 0)
    
    def test_better_encoder_default(self):
        result = stringify(normal='hello world')
        assert_true(result.find('"normal"') >= 0)
        assert_true(result.find('normal') >= 0)