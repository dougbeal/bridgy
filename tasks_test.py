#!/usr/bin/python
"""Unit tests for tasks.py.
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import datetime
import mox
import urlparse

import models
import models_test
import tasks
from tasks import Poll, Propagate
import testutil
import util

from google.appengine.ext import db
from google.appengine.ext import webapp


class TaskQueueTest(testutil.ModelsTest):
  """Attributes:
    task_params: the query parameters passed in the task POST request
    post_url: the URL for post_task() to post to
    now: the datetime to be returned by datetime.now()
  """
  task_params = None
  post_url = None
  now = datetime.datetime.now()

  def post_task(self, expected_status=200):
    """Runs post(), injecting self.now to be returned by datetime.now().

    Args:
      expected_status: integer, the expected HTTP return code
    """
    poll_with_now = lambda: Poll(now=lambda: self.now)
    propagate_with_now = lambda: Propagate(now=lambda: self.now)
    application = webapp.WSGIApplication([
        ('/_ah/queue/poll', poll_with_now),
        ('/_ah/queue/propagate', propagate_with_now),
        ])

    super(TaskQueueTest, self).post(application,
                                    self.post_url,
                                    expected_status,
                                    post_params=self.task_params)


class PollTest(TaskQueueTest):

  post_url = '/_ah/queue/poll'

  def setUp(self):
    super(PollTest, self).setUp()
    self.task_params = {'source_key': self.sources[0].key(),
                        'last_polled': '1970-01-01-00-00-00'}
    self.orig_destinations = tasks.DESTINATIONS
    tasks.DESTINATIONS = ['FakeDestination']

  def tearDown(self):
    tasks.DESTINATIONS = self.orig_destinations
    super(PollTest, self).tearDown()

  def assert_comments(self):
    """Asserts that all of self.comments are saved."""
    self.assert_entities_equal(self.comments, models.Comment.all())

  def test_poll(self):
    """A normal poll task."""
    self.assertEqual([], list(models.Comment.all()))
    self.assertEqual([], self.taskqueue_stub.GetTasks('poll'))

    self.post_task()
    self.assert_comments()

    source = db.get(self.sources[0].key())
    self.assertEqual(self.now, source.last_polled)

    tasks = self.taskqueue_stub.GetTasks('poll')
    self.assertEqual(1, len(tasks))
    self.assertEqual('/_ah/queue/poll', tasks[0]['url'])

    params = testutil.get_task_params(tasks[0])
    self.assertEqual(str(source.key()),
                     params['source_key'])
    self.assertEqual(self.now.strftime(util.POLL_TASK_DATETIME_FORMAT),
                     params['last_polled'])

  def test_existing_comments(self):
    """Poll should be idempotent and not touch existing comment entities.
    """
    self.comments[0].status = 'complete'
    self.comments[0].save()

    self.post_task()
    self.assert_comments()
    self.assertEqual('complete', db.get(self.comments[0].key()).status)

  def test_wrong_last_polled(self):
    """If the source doesn't have our last polled value, we should quit.
    """
    self.sources[0].last_polled = datetime.datetime.utcfromtimestamp(3)
    self.sources[0].save()
    self.post_task()
    self.assertEqual([], list(models.Comment.all()))


class PropagateTest(TaskQueueTest):

  post_url = '/_ah/queue/propagate'

  def setUp(self):
    super(PropagateTest, self).setUp()
    self.comments[0].save()
    self.task_params = {'comment_key': self.comments[0].key()}

  def assert_comment_is(self, status, leased_until=False):
    """Asserts that comments[0] has the given values in the datastore.
    """
    comment = db.get(self.comments[0].key())
    self.assertEqual(status, comment.status)
    if leased_until is not False:
      self.assertEqual(leased_until, comment.leased_until)

  def test_propagate(self):
    """A normal propagate task."""
    self.assertEqual('new', self.comments[0].status)
    dest = self.comments[0].dest
    self.assertEqual([], dest.get_comments())

    self.post_task()
    self.assert_keys_equal([self.comments[0]], dest.get_comments())
    self.assert_comment_is('complete', self.now + Propagate.LEASE_LENGTH)

  def test_already_complete(self):
    """If the comment has already been propagated, do nothing."""
    self.comments[0].status = 'complete'
    self.comments[0].save()

    self.post_task()
    self.assertEqual([], self.comments[0].dest.get_comments())
    self.assert_comment_is('complete')

  def test_leased(self):
    """If the comment is processing and the lease hasn't expired, do nothing."""
    self.comments[0].status = 'processing'
    leased_until = self.now + datetime.timedelta(minutes=1)
    self.comments[0].leased_until = leased_until
    self.comments[0].save()

    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    self.assertEqual([], self.comments[0].dest.get_comments())
    self.assert_comment_is('processing', leased_until)

    comment = db.get(self.comments[0].key())
    self.assertEqual('processing', comment.status)
    self.assertEqual(leased_until, comment.leased_until)

  def test_lease_expired(self):
    """If the comment is processing but the lease has expired, process it."""
    self.comments[0].status = 'processing'
    self.comments[0].leased_until = self.now - datetime.timedelta(minutes=1)
    self.comments[0].save()

    self.post_task()
    self.assert_keys_equal([self.comments[0]], self.comments[0].dest.get_comments())
    self.assert_comment_is('complete', self.now + Propagate.LEASE_LENGTH)

  def test_no_comment(self):
    """If the comment doesn't exist, the request should fail."""
    self.comments[0].delete()
    self.post_task(expected_status=Propagate.ERROR_HTTP_RETURN_CODE)
    self.assertEqual([], self.comments[0].dest.get_comments())

  def test_exceptions(self):
    """If any part raises an exception, the lease should be released."""
    methods = [
      (Propagate, 'lease_comment', []),
      (Propagate, 'complete_comment', []),
      (testutil.FakeDestination, 'add_comment', [mox.IgnoreArg()]),
      ]

    for cls, method, args in methods:
      self.mox.UnsetStubs()
      self.mox.StubOutWithMock(cls, method)
      getattr(cls, method)(*args).AndRaise(Exception('foo'))
      self.mox.ReplayAll()

      self.post_task(expected_status=500)
      self.assert_comment_is('new', None)
      self.mox.VerifyAll()
