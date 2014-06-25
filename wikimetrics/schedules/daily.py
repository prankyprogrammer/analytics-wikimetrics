import traceback
from celery import group, chain
from celery.utils.log import get_task_logger

from wikimetrics.configurables import queue
from wikimetrics.utils import chunk


task_logger = get_task_logger(__name__)


@queue.task()
def recurring_reports(report_id=None):
    from wikimetrics.configurables import db
    from wikimetrics.models import ReportStore, RunReport
    
    try:
        session = db.get_session()
        query = session.query(ReportStore) \
            .filter(ReportStore.recurrent) \
        
        if report_id is not None:
            query = query.filter(ReportStore.id == report_id)

        parallelism = int(queue.conf.get('MAX_PARALLEL_PER_RUN'))
        new_report_runs = []
        for report in query.all():
            try:
                task_logger.info('Running recurring report "{0}"'.format(report))
                no_more_than = queue.conf.get('MAX_INSTANCES_PER_RECURRENT_REPORT')
                kwargs = dict()
                if no_more_than:
                    kwargs['no_more_than'] = no_more_than
                new_report_runs += list(RunReport.create_reports_for_missed_days(
                    report,
                    session,
                    **kwargs
                ))
            except Exception:
                task_logger.error('Problem running recurring report "{}": {}'.format(
                    report, traceback.format_exc()
                ))

        groups = chunk(new_report_runs, parallelism)
        chain_of_groups = chain([group([r.task.s(r) for r in g]) for g in groups])
        chain_of_groups.delay()

    except Exception:
        task_logger.error('Problem running recurring reports: {}'.format(
            traceback.format_exc()
        ))
    finally:
        session.close()
