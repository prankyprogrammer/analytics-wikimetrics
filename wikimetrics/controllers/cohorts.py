import json
from flask import url_for, flash, render_template, redirect, request, jsonify
from flask.ext.login import current_user
from sqlalchemy.sql import exists
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound
from ..utils import json_response, json_error, json_redirect
from ..configurables import app, db
from ..models import (
    Cohort, CohortUser, CohortUserRole,
    User, WikiUser, CohortWikiUser, MediawikiUser
)
import logging

logger = logging.getLogger(__name__)


@app.route('/cohorts/')
def cohorts_index():
    """
    Renders a page with a list cohorts belonging to the currently logged in user.
    If the user is an admin, she has the option of seeing other users' cohorts.
    """
    return 'cohorts'


@app.route('/cohorts/list/')
def cohorts_list():
    db_session = db.get_session()
    cohorts = db_session.query(Cohort.id, Cohort.name, Cohort.description)\
        .join(CohortUser)\
        .join(User)\
        .filter(User.id == current_user.id)\
        .filter(CohortUser.role.in_([CohortUserRole.OWNER, CohortUserRole.VIEWER]))\
        .filter(Cohort.enabled)\
        .all()
    return jsonify(cohorts=cohorts)


@app.route('/cohorts/detail/<string:name_or_id>')
def cohort_detail(name_or_id):
    """
    Returns a JSON object of the form:
    {id: 2, name: 'Berlin Beekeeping Society', description: '', wikiusers: [
        {mediawiki_username: 'Andrea', mediawiki_userid: 5, project: 'dewiki'},
        {mediawiki_username: 'Dennis', mediawiki_userid: 6, project: 'dewiki'},
        {mediawiki_username: 'Florian', mediawiki_userid: 7, project: 'dewiki'},
        {mediawiki_username: 'Gabriele', mediawiki_userid: 8, project: 'dewiki'},
    ]}
    """
    cohort = None
    if str(name_or_id).isdigit():
        cohort = get_cohort_by_id(int(name_or_id))
    else:
        cohort = get_cohort_by_name(name_or_id)
    
    if cohort:
        cohort_with_wikiusers = populate_cohort_wikiusers(cohort)
        return json_response(cohort_with_wikiusers)
    
    return '{}', 404


def get_cohort_query():
    db_session = db.get_session()
    return db_session.query(Cohort)\
        .join(CohortUser)\
        .join(User)\
        .filter(User.id == current_user.id)\
        .filter(CohortUser.role.in_([CohortUserRole.OWNER, CohortUserRole.VIEWER]))\
        .filter(Cohort.enabled)


def get_cohort_by_id(id):
    try:
        return get_cohort_query().filter(Cohort.id == id).one()
    # MultipleResultsFound NoResultFound
    except:
        return None


def get_cohort_by_name(name):
    try:
        return get_cohort_query().filter(Cohort.name == name).one()
    # MultipleResultsFound NoResultFound
    except:
        return None


def populate_cohort_wikiusers(cohort):
    db_session = db.get_session()
    wikiusers = db_session.query(WikiUser)\
        .join(CohortWikiUser)\
        .filter(CohortWikiUser.cohort_id == cohort.id)\
        .all()
    cohort_dict = cohort._asdict()
    cohort_dict['wikiusers'] = [wu._asdict() for wu in wikiusers]
    return cohort_dict


@app.route('/cohorts/upload', methods=['GET','POST'])
def cohort_upload():
    """ View for uploading and validating a new cohort via CSV """
    if request.method == 'GET':
        return render_template('csv_upload.html')

    elif request.method == 'POST':
        try:
            csv = request.files['csv']
            name = request.form['name']
            project = request.form['project']
            if not csv or not name or len(name) is 0:
                flash('The form was invalid, please select a file and name the cohort.')
                return redirect(url_for('cohort_upload'))
            
            if get_cohort_by_name(name):
                flash('That Cohort name is already taken.')
                return redirect(url_for('cohort_upload'))
            
            unparsed = csv.reader(normalize_newlines(csv.stream))
            unvalidated = parse_records(unparsed, project)
            (valid, invalid) = validate_records(unvalidated)
            
            return render_template(
                'csv_upload_review.html',
                valid=valid,
                invalid=invalid,
                valid_json=to_safe_json(valid),
                invalid_json=to_safe_json(invalid),
                name=name,
                project=project,
            )
        except Exception, e:
            logging.exception(str(e))
            flash(
                'The file you uploaded was not in a valid format, could not be validated,'
                'or the project you specified is not configured on this instance of Wiki Metrics.'
            )
            return redirect(url_for('cohort_upload'))


@app.route('/cohorts/create', methods=['POST'])
def cohort_upload_finish():
    try:
        name = request.form.get('name')
        project = request.form.get('project')
        users_json = request.form.get('users')
        users = json.loads(users_json)
        # re-validate
        if get_cohort_by_name(name):
            raise Exception('Cohort name {0} is already used'.format(name))
        
        # TODO: re-enable validation when either
        # 1. the site is used by external, potentially untrusted users
        # 2. the performance of validation is improved
        #(valid, invalid) = validate_records(users)
        #if invalid:
            #raise Exception('Cohort changed since last validation')
        # save the cohort
        valid = users
        
        if not project:
            if all([user['project'] == users[0]['project'] for user in users]):
                project = users[0]['project']
        logging.debug('adding cohort: {0}, with project: {1}'.format(name, project))
        cohort = create_cohort(name, 'TODO: add description', project, valid)
        return url_for('cohort_details', name_or_id=cohort.id)
        
    except Exception, e:
        logging.exception(str(e))
        flash('There was a problem finishing the upload.  The cohort was not saved.')
        return '<<error>>'


def create_cohort(name, description, project, valid_users):
    db_session = db.get_session()
    cohort = Cohort(
        name=name,
        default_project=project,
        description=description,
    )
    db_session.add(cohort)
    db_session.commit()
    
    cohort_owner = CohortUser(
        cohort_id=cohort.id,
        user_id=current_user.id,
    )
    db_session.add(cohort_owner)
    
    wikiusers = []
    for valid_user in valid_users:
        wikiuser = WikiUser(
            mediawiki_userid=valid_user['userid'],
            mediawiki_username=valid_user['username'],
        )
        wikiusers.append(wikiuser)
    db_session.add_all(wikiusers)
    db_session.commit()
    
    cohort_wikiusers = []
    for wikiuser in wikiusers:
        cohort_wikiuser = CohortWikiUser(
            cohort_id=cohort.id,
            wiki_user_id=wikiuser.id,
        )
        cohort_wikiusers.append(cohort_wikiuser)
    db_session.add_all(cohort_wikiusers)
    db_session.commit()


@app.route('/cohorts/validate/name')
def validate_cohort_name_allowed():
    name = request.args.get('name')
    available = get_cohort_by_name(name) is None
    return json.dumps(available)


def normalize_newlines(stream):
    for line in stream:
        if '\r' in line:
            for tok in line.split('\r'):
                yield tok
        else:
            yield line


def to_safe_json(s):
    return json.dumps(s).replace("'", "\\'").replace('"', '\\"')


def parse_records(records, default_project):
    # NOTE: the reason for the crazy -1 and comma joins
    # is that some users can have commas in their name
    # TODO: This makes it impossible to add fields to the csv in the future,
    # so maybe require the project to be the first field and the username to be the last
    # or maybe change to a tsv format
    return [{
        'username': parse_username(",".join([str(p) for p in r[:-1]])),
        'project': r[-1].decode('utf8') if len(r) > 1 else default_project
    } for r in records if r]


def parse_username(raw_name):
    stripped = str(raw_name).decode('utf8').strip()
    # unfortunately .title() or .capitalize() don't work
    # because 'miliMetric'.capitalize() == 'Milimetric'
    return stripped[0].upper() + stripped[1:]


def normalize_project(project):
    project = project.strip().lower()
    if project in db.project_host_map:
        return project
    else:
        # try adding wiki to end
        new_proj = project + 'wiki'
        if new_proj not in db.project_host_map:
            return None
        else:
            return new_proj


def get_wikiuser_by_name(username, project):
    # NOTE: Not needed right? username = username.encode('utf-8')
    db_session = db.get_mw_session(project)
    try:
        return db_session.query(MediawikiUser)\
            .filter(MediawikiUser.user_name == username)\
            .one()
    except:
        return None


def get_wikiuser_by_id(id, project):
    db_session = db.get_mw_session(project)
    try:
        return db_session.query(MediawikiUser)\
            .filter(MediawikiUser.user_id == id)\
            .one()
    except:
        return None


def normalize_user(user_str, project):
    wikiuser = get_wikiuser_by_name(user_str, project)
    if wikiuser is not None:
        return (wikiuser.user_id, wikiuser.user_name)
    
    if not user_str.isdigit():
        return None
    
    wikiuser = get_wikiuser_by_id(user_str, project)
    if wikiuser is not None:
        return (wikiuser.user_id, wikiuser.user_name)
    
    return None


def deduplicate(list_of_objects, key_function):
    uniques = dict()
    for o in list_of_objects:
        key = key_function(o)
        if not key in uniques:
            uniques[key] = o
    
    return uniques.values()


def project_name_for_link(project):
    if project.endswith('wiki'):
        return project[:len(project) - 4]
    return project


def link_to_user_page(username, project):
    project = project_name_for_link(project)
    return 'https://%s.wikipedia.org/wiki/User:%s' % (project, username)


def validate_records(records):
    valid = []
    invalid = []
    for record in records:
        record['user_str'] = record['username']
        normalized_project = normalize_project(record['project'])
        if normalized_project is None:
            record['reason_invalid'] = 'invalid project: %s' % record['project']
            invalid.append(record)
            continue
        normalized_user = normalize_user(record['user_str'], normalized_project)
        # make a link to the potential user page even if user doesn't exist
        # this gives a chance to see any misspelling etc.
        record['link'] = link_to_user_page(record['username'], normalized_project)
        if normalized_user is None:
            logging.debug('invalid: %s', record['user_str'])
            record['reason_invalid'] = 'invalid user_name / user_id: %s' % record['user_str']
            invalid.append(record)
            continue
        # set the normalized values and append to valid
        logging.debug('found a valid user_str: %s', record['user_str'])
        record['project'] = normalized_project
        record['user_id'], record['username'] = normalized_user
        valid.append(record)
    
    valid = deduplicate(valid, lambda record: record['username'])
    return (valid, invalid)
