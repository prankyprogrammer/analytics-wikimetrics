from flask import render_template, request, url_for, Response
from flask.ext.login import current_user
import celery
from celery.task.control import revoke
from ..configurables import app, db
from ..models import Report, RunReport, PersistentReport
from ..models.report_nodes import Aggregation
from ..metrics import metric_classes
from ..utils import json_response, json_error, json_redirect, thirty_days_ago
import json
from StringIO import StringIO
from csv import DictWriter


@app.route('/reports/')
def reports_index():
    """
    Renders a page with a list of reports started by the currently logged in user.
    If the user is an admin, she has the option to see other users' reports.
    """
    return render_template('reports.html')


@app.route('/reports/create/', methods=['GET', 'POST'])
def reports_request():
    """
    Renders a page that facilitates kicking off a new report
    """
    
    if request.method == 'GET':
        return render_template('report.html')
    else:
        desired_responses = json.loads(request.form['responses'])
        jr = RunReport(desired_responses, user_id=current_user.id)
        
        async_response = jr.task.delay(jr)
        app.logger.info(
            'starting report with celery id: %s, PersistentReport.id: %d',
            async_response.task_id, jr.persistent_id
        )
        
        #return render_template('reports.html')
        return json_redirect(url_for('reports_index'))


@app.route('/reports/list/')
def reports_list():
    db_session = db.get_session()
    reports = db_session.query(PersistentReport)\
        .filter(PersistentReport.user_id == current_user.id)\
        .filter(PersistentReport.created > thirty_days_ago())\
        .filter(PersistentReport.show_in_ui)\
        .all()
    # TODO: update status for all reports at all times (not just show_in_ui ones)
    # update status for each report
    for report in reports:
        report.update_status()
    
    # TODO fix json_response to deal with PersistentReport objects
    reports_json = json_response(reports=[report._asdict() for report in reports])
    db_session.close()
    return reports_json


@app.route('/reports/status/<task_id>')
def report_status(task_id):
    celery_task = Report.task.AsyncResult(task_id)
    return json_response(status=celery_task.status)


@app.route('/reports/result/<task_id>.csv')
def report_result_csv(task_id):
    celery_task = Report.task.AsyncResult(task_id)
    if not celery_task:
        return json_error('no task exists with id: {0}'.format(task_id))
    
    if celery_task.ready():
        task_result = celery_task.get()
        
        csv_io = StringIO()
        if task_result:
            columns = []
            
            if Aggregation.IND in task_result:
                columns = task_result[Aggregation.IND][0].values()[0].keys()
            elif Aggregation.SUM in task_result:
                columns = task_result[Aggregation.SUM].keys()
            elif Aggregation.AVG in task_result:
                columns = task_result[Aggregation.AVG].keys()
            elif Aggregation.STD in task_result:
                columns = task_result[Aggregation.STD].keys()
            
            # if task_result is not empty find header in first row
            fieldnames = ['user_id'] + columns
        else:
            fieldnames = ['user_id']
        writer = DictWriter(csv_io, fieldnames)
        
        # collect rows to output in CSV
        task_rows = []
        
        # Individual Results
        if Aggregation.IND in task_result:
            # fold user_id into dict so we can use DictWriter to escape things
            for user_id, row in task_result[Aggregation.IND][0].iteritems():
                task_row = row.copy()
                task_row['user_id'] = user_id
                task_rows.append(task_row)
        
        # Aggregate Results
        if Aggregation.SUM in task_result:
            task_row = task_result[Aggregation.SUM].copy()
            task_row['user_id'] = Aggregation.SUM
            task_rows.append(task_row)
        
        if Aggregation.AVG in task_result:
            task_row = task_result[Aggregation.AVG].copy()
            task_row['user_id'] = Aggregation.AVG
            task_rows.append(task_row)
        
        if Aggregation.STD in task_result:
            task_row = task_result[Aggregation.STD].copy()
            task_row['user_id'] = Aggregation.STD
            task_rows.append(task_row)
        
        writer.writeheader()
        writer.writerows(task_rows)
        return Response(csv_io.getvalue(), mimetype='text/csv')
    else:
        return json_response(status=celery_task.status)


@app.route('/reports/result/<task_id>.json')
def report_result_json(task_id):
    celery_task = Report.task.AsyncResult(task_id)
    if not celery_task:
        return json_error('no task exists with id: {0}'.format(task_id))
    
    if celery_task.ready():
        task_result = celery_task.get()
        
        # get the parameters from the database
        db_session = db.get_session()
        report = db_session.query(PersistentReport)\
            .filter(PersistentReport.result_key == task_id)\
            .one()
        parameters = report.parameters
        db_session.close()
        
        return json_response(
            result=task_result,
            parameters=json.loads(parameters),
        )
    else:
        return json_response(status=celery_task.status)


@app.route('/reports/kill/<task_id>')
def report_kill(task_id):
    return 'not implemented'
    #db_session = db.get_session()
    #db_report = db_session.query(PersistentReport).get(task_id)
    #if not db_report:
        #return json_error('no task exists with id: {0}'.format(task_id))
    #celery_task = Report.task.AsyncResult(db_report.result_key)
    #app.logger.debug('revoking task: %s', celery_task.id)
    #celery_task.revoke()
    # TODO figure out how to terminate tasks. this throws an error
    # which I believe is related to https://github.com/celery/celery/issues/1153
    # and which is fixed by a patch.  however, I can't get things running
    # with development version
    #revoke(celery_task.id, terminate=True)
    #return json_response(status=celery_task.status)