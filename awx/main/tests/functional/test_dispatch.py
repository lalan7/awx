import datetime
import multiprocessing
import random
import signal
import time

from django.utils.timezone import now as tz_now
import pytest

from awx.main.models import Job, WorkflowJob, Instance
from awx.main.dispatch import reaper
from awx.main.dispatch.pool import WorkerPool, AutoscalePool
from awx.main.dispatch.publish import task
from awx.main.dispatch.worker import BaseWorker, TaskWorker


@task()
def add(a, b):
    return a + b


@task()
class Adder:
    def run(self, a, b):
        return add(a, b)


@task(queue='hard-math')
def multiply(a, b):
    return a * b


class SimpleWorker(BaseWorker):

    def perform_work(self, body, *args):
        pass


class ResultWriter(BaseWorker):

    def perform_work(self, body, result_queue):
        result_queue.put(body + '!!!')


class SlowResultWriter(BaseWorker):

    def perform_work(self, body, result_queue):
        time.sleep(3)
        super(SlowResultWriter, self).perform_work(body, result_queue)


@pytest.mark.django_db
class TestWorkerPool:

    def setup_method(self, test_method):
        self.pool = WorkerPool(min_workers=3)

    def teardown_method(self, test_method):
        self.pool.stop(signal.SIGTERM)

    def test_worker(self):
        self.pool.init_workers(SimpleWorker().work_loop)
        assert len(self.pool) == 3
        for worker in self.pool.workers:
            assert worker.messages_sent == 0
            assert worker.alive is True

    def test_single_task(self):
        self.pool.init_workers(SimpleWorker().work_loop)
        self.pool.write(0, 'xyz')
        assert self.pool.workers[0].messages_sent == 1  # worker at index 0 handled one task
        assert self.pool.workers[1].messages_sent == 0
        assert self.pool.workers[2].messages_sent == 0

    def test_queue_preference(self):
        self.pool.init_workers(SimpleWorker().work_loop)
        self.pool.write(2, 'xyz')
        assert self.pool.workers[0].messages_sent == 0
        assert self.pool.workers[1].messages_sent == 0
        assert self.pool.workers[2].messages_sent == 1  # worker at index 2 handled one task

    def test_worker_processing(self):
        result_queue = multiprocessing.Queue()
        self.pool.init_workers(ResultWriter().work_loop, result_queue)
        for i in range(10):
            self.pool.write(
                random.choice(range(len(self.pool))),
                'Hello, Worker {}'.format(i)
            )
        all_messages = [result_queue.get(timeout=1) for i in range(10)]
        all_messages.sort()
        assert all_messages == [
            'Hello, Worker {}!!!'.format(i)
            for i in range(10)
        ]

        total_handled = sum([worker.messages_sent for worker in self.pool.workers])
        assert total_handled == 10


@pytest.mark.django_db
class TestAutoScaling:

    def setup_method(self, test_method):
        self.pool = AutoscalePool(min_workers=2, max_workers=10)

    def teardown_method(self, test_method):
        self.pool.stop(signal.SIGTERM)

    def test_scale_up(self):
        result_queue = multiprocessing.Queue()
        self.pool.init_workers(SlowResultWriter().work_loop, result_queue)

        # start with two workers, write an event to each worker and make it busy
        assert len(self.pool) == 2
        for i, w in enumerate(self.pool.workers):
            w.put('Hello, Worker {}'.format(0))
        assert len(self.pool) == 2

        # wait for the subprocesses to start working on their tasks and be marked busy
        time.sleep(1)
        assert self.pool.should_grow

        # write a third message, expect a new worker to spawn because all
        # workers are busy
        self.pool.write(0, 'Hello, Worker {}'.format(2))
        assert len(self.pool) == 3

    def test_scale_down(self):
        self.pool.init_workers(ResultWriter().work_loop, multiprocessing.Queue())

        # start with two workers, and scale up to 10 workers
        assert len(self.pool) == 2
        for i in range(8):
            self.pool.up()
        assert len(self.pool) == 10

        # cleanup should scale down to 8 workers
        self.pool.cleanup()
        assert len(self.pool) == 2

    def test_max_scale_up(self):
        self.pool.init_workers(ResultWriter().work_loop, multiprocessing.Queue())

        assert len(self.pool) == 2
        for i in range(25):
            self.pool.up()
        assert self.pool.max_workers == 10
        assert self.pool.full is True
        assert len(self.pool) == 10

    def test_equal_worker_distribution(self):
        # if all workers are busy, spawn new workers *before* adding messages
        # to an existing queue
        self.pool.init_workers(SlowResultWriter().work_loop, multiprocessing.Queue)

        # start with two workers, write an event to each worker and make it busy
        assert len(self.pool) == 2
        for i in range(10):
            self.pool.write(0, 'Hello, World!')
        assert len(self.pool) == 10
        for w in self.pool.workers:
            assert w.busy
            assert len(w.managed_tasks) == 1

        # the queue is full at 10, the _next_ write should put the message into
        # a worker's backlog
        assert len(self.pool) == 10
        for w in self.pool.workers:
            assert w.messages_sent == 1
        self.pool.write(0, 'Hello, World!')
        assert len(self.pool) == 10
        assert self.pool.workers[0].messages_sent == 2

    def test_lost_worker_autoscale(self):
        # if a worker exits, it should be replaced automatically up to min_workers
        self.pool.init_workers(ResultWriter().work_loop, multiprocessing.Queue())

        # start with two workers, kill one of them
        assert len(self.pool) == 2
        assert not self.pool.should_grow
        alive_pid = self.pool.workers[1].pid
        self.pool.workers[0].process.terminate()
        time.sleep(1)  # wait a moment for sigterm

        # clean up and the dead worker
        self.pool.cleanup()
        assert len(self.pool) == 1
        assert self.pool.workers[0].pid == alive_pid

        # the next queue write should replace the lost worker
        self.pool.write(0, 'Hello, Worker')
        assert len(self.pool) == 2


class TestTaskDispatcher:

    @property
    def tm(self):
        return TaskWorker()

    def test_function_dispatch(self):
        result = self.tm.perform_work({
            'task': 'awx.main.tests.functional.test_dispatch.add',
            'args': [2, 2]
        })
        assert result == 4

    def test_method_dispatch(self):
        result = self.tm.perform_work({
            'task': 'awx.main.tests.functional.test_dispatch.Adder',
            'args': [2, 2]
        })
        assert result == 4


class TestTaskPublisher:

    def test_function_callable(self):
        assert add(2, 2) == 4

    def test_method_callable(self):
        assert Adder().run(2, 2) == 4

    def test_function_apply_async(self):
        message, queue = add.apply_async([2, 2])
        assert message['args'] == [2, 2]
        assert message['kwargs'] == {}
        assert message['task'] == 'awx.main.tests.functional.test_dispatch.add'
        assert queue == 'awx_private_queue'

    def test_method_apply_async(self):
        message, queue = Adder.apply_async([2, 2])
        assert message['args'] == [2, 2]
        assert message['kwargs'] == {}
        assert message['task'] == 'awx.main.tests.functional.test_dispatch.Adder'
        assert queue == 'awx_private_queue'

    def test_apply_with_queue(self):
        message, queue = add.apply_async([2, 2], queue='abc123')
        assert queue == 'abc123'

    def test_queue_defined_in_task_decorator(self):
        message, queue = multiply.apply_async([2, 2])
        assert queue == 'hard-math'

    def test_queue_overridden_from_task_decorator(self):
        message, queue = multiply.apply_async([2, 2], queue='not-so-hard')
        assert queue == 'not-so-hard'

    def test_apply_with_callable_queuename(self):
        message, queue = add.apply_async([2, 2], queue=lambda: 'called')
        assert queue == 'called'


yesterday = tz_now() - datetime.timedelta(days=1)


@pytest.mark.django_db
class TestJobReaper(object):

    @pytest.mark.parametrize('status, execution_node, controller_node, modified, fail', [
        ('running', '', '', None, False),        # running, not assigned to the instance
        ('running', 'awx', '', None, True),      # running, has the instance as its execution_node
        ('running', '', 'awx', None, True),      # running, has the instance as its controller_node
        ('waiting', '', '', None, False),        # waiting, not assigned to the instance
        ('waiting', 'awx', '', None, False),     # waiting, was edited less than a minute ago
        ('waiting', '', 'awx', None, False),     # waiting, was edited less than a minute ago
        ('waiting', 'awx', '', yesterday, True), # waiting, assigned to the execution_node, stale
        ('waiting', '', 'awx', yesterday, True), # waiting, assigned to the controller_node, stale
    ])
    def test_should_reap(self, status, fail, execution_node, controller_node, modified):
        i = Instance(hostname='awx')
        i.save()
        j = Job(
            status=status,
            execution_node=execution_node,
            controller_node=controller_node,
            start_args='SENSITIVE',
        )
        j.save()
        if modified:
            # we have to edit the modification time _without_ calling save()
            # (because .save() overwrites it to _now_)
            Job.objects.filter(id=j.id).update(modified=modified)
        reaper.reap(i)
        job = Job.objects.first()
        if fail:
            assert job.status == 'failed'
            assert 'marked as failed' in job.job_explanation
            assert job.start_args == ''
        else:
            assert job.status == status

    def test_workflow_does_not_reap(self):
        i = Instance(hostname='awx')
        i.save()
        j = WorkflowJob(
            status='running',
            execution_node='awx'
        )
        j.save()
        reaper.reap(i)

        assert WorkflowJob.objects.first().status == 'running'
