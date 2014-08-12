import json
from flask import url_for, flash, render_template, redirect, request, g
from flask.ext.login import current_user
from sqlalchemy import func
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound
from sqlalchemy.sql.expression import or_

from wikimetrics.utils import (
    json_response, json_error, json_redirect, deduplicate_by_key, parse_tag
)
from wikimetrics.exceptions import Unauthorized, DatabaseError
from wikimetrics.configurables import app, db
from wikimetrics.forms import CohortUpload
from wikimetrics.models import (
    CohortStore, CohortUserStore, UserStore, WikiUserStore, CohortWikiUserStore,
    MediawikiUser, ValidateCohort, TagStore, CohortTagStore
)
from wikimetrics.enums import CohortUserRole
from wikimetrics.api import CohortService, TagService


# TODO: because this is injected by the tests into the REAL controller, it is
# currently impossible to integration test the controller to make sure it
# adds the service properly.  This needs to be addressed but I want to stop
# hacking at the code and do proper dependency injection.
@app.before_request
def setup_cohort_service():
    if request.endpoint is not None:
        if request.path.startswith('/cohorts'):
            cohort_service = getattr(g, 'cohort_service', None)
            tag_service = getattr(g, 'tag_service', None)
            if cohort_service is None:
                g.cohort_service = CohortService()
            if tag_service is None:
                g.tag_service = TagService()


@app.route('/cohorts/')
def cohorts_index():
    """
    Renders a page with a list cohorts belonging to the currently logged in user.
    If the user is an admin, she has the option of seeing other users' cohorts.
    """
    session = db.get_session()
    tags = g.tag_service.get_all_tags(session)
    return render_template('cohorts.html', tags=json.dumps(tags))


@app.route('/cohorts/list/')
def cohorts_list():
    include_invalid = request.args.get('include_invalid', 'false') == 'true'
    db_session = db.get_session()
    if include_invalid:
        cohorts = g.cohort_service.get_list_for_display(db_session, current_user.id)
    else:
        cohorts = g.cohort_service.get_list(db_session, current_user.id)

    return json_response(cohorts=[{
        'id': c.id,
        'name': c.name,
        'description': c.description,
    } for c in cohorts])


@app.route('/cohorts/detail/invalid-users/<int:cohort_id>')
def cohort_invalid_detail(cohort_id):
    session = db.get_session()
    try:
        cohort = g.cohort_service.get_for_display(
            session, current_user.id, by_id=cohort_id
        )
        wikiusers = session\
            .query(WikiUserStore.mediawiki_username, WikiUserStore.reason_invalid)\
            .filter(WikiUserStore.validating_cohort == cohort.id) \
            .filter(WikiUserStore.valid.in_([False, None])) \
            .all()
        return json_response(invalid_wikiusers=[wu._asdict() for wu in wikiusers])
    except Exception, e:
        # don't need to roll back session because it's just a query
        app.logger.exception(str(e))
        return json_error('Error fetching invalid users for {0}'.format(cohort_id))


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
    db_session = db.get_session()
    try:
        kargs = dict()
        if str(name_or_id).isdigit():
            kargs['by_id'] = int(name_or_id)
        else:
            kargs['by_name'] = name_or_id
        cohort = g.cohort_service.get_for_display(db_session, current_user.id, **kargs)

        cohort_dict = cohort.__dict__
        cohort_dict['tags'] = populate_cohort_tags(cohort.id, db_session)

        cohort_dict['validation'] =\
            populate_cohort_validation_status(cohort, db_session, cohort.size)

    # don't need to roll back session because it's just a query
    except Unauthorized:
        return 'You are not allowed to access this Cohort', 401
    except NoResultFound:
        return 'Could not find this Cohort', 404

    return json_response(cohort_dict)


def populate_cohort_validation_status(cohort, db_session, number_of_wikiusers):
    """
    Fetches the validation information for the cohort
    returns an empty dictionary if the cohort does not have validation information
    """
    validation = {}

    if cohort.has_validation_info:

        task_key = cohort.validation_queue_key

        if not task_key:
            validation['validation_status'] = 'UNKNOWN'
            #TODO do these defaults really make sense?
            validation['validated_count'] = number_of_wikiusers
            validation['total_count'] = number_of_wikiusers
            validation['valid_count'] = validation['total_count']
            validation['invalid_count'] = 0
            validation['delete_message'] = None

        else:
            validation = g.cohort_service.get_validation_info(cohort, db_session)
            validation_task = ValidateCohort.task.AsyncResult(task_key)
            validation['validation_status'] = validation_task.status
            # celery returns 'PENDING' for unknown task ids
            # if we are looking at a task after a restart it will be an unknown one
            if (validation['total_count'] == validation['validated_count']):
                validation['validation_status'] = "SUCCESS"
            
            users = num_users(db_session, cohort.id)
            non_owners = users - 1
            role = get_role(db_session, cohort.id)
            if users != 1 and role == CohortUserRole.OWNER:
                validation['delete_message'] = 'delete this cohort? ' + \
                    'There are {0} other user(s) shared on this \
                    cohort.'.format(non_owners)
            else:
                validation['delete_message'] = 'delete this cohort?'

    return validation


def populate_cohort_tags(cohort_id, db_session):
    tags = db_session.query(TagStore) \
        .filter(CohortTagStore.cohort_id == cohort_id) \
        .filter(CohortTagStore.tag_id == TagStore.id) \
        .all()

    return [t._asdict() for t in tags]


@app.route('/cohorts/upload', methods=['GET', 'POST'])
def cohort_upload():
    """ View for uploading and validating a new cohort via CSV """
    form = CohortUpload()

    if request.method == 'POST':
        form = CohortUpload.from_request(request)
        try:
            if not form.validate():
                flash('Please fix validation problems.', 'warning')
            elif get_cohort_by_name(form.name.data):
                flash('That Cohort name is already taken.', 'warning')
            else:
                form.parse_records()
                vc = ValidateCohort.from_upload(form, current_user.id)
                vc.task.delay(vc)
                return redirect('{0}#{1}'.format(
                    url_for('cohorts_index'),
                    vc.cohort_id
                ))
        except Exception, e:
            app.logger.exception(str(e))
            flash('Server error while processing your upload', 'error')

    return render_template(
        'csv_upload.html',
        projects=json.dumps(sorted(db.get_project_host_map().keys())),
        form=form,
    )


def get_cohort_by_name(name):
    """
    Gets a cohort by name, without checking access or worrying about duplicates
    """
    db_session = db.get_session()
    return db_session.query(CohortStore).filter(CohortStore.name == name).first()


@app.route('/cohorts/validate/name')
def validate_cohort_name_allowed():
    name = request.args.get('name')
    available = get_cohort_by_name(name) is None
    return json.dumps(available)


@app.route('/cohorts/validate/project')
def validate_cohort_project_allowed():
    project = request.args.get('project')
    valid = project in db.get_project_host_map()
    return json.dumps(valid)


@app.route('/cohorts/validate/<int:cohort_id>', methods=['POST'])
def validate_cohort(cohort_id):
    name = None
    session = db.get_session()
    try:
        c = g.cohort_service.get_for_display(session, current_user.id, by_id=cohort_id)
        name = c.name
        # TODO we need some kind of global config that is not db specific
        vc = ValidateCohort(c)
        vc.task.delay(vc)
        return json_response(message='Validating cohort "{0}"'.format(name))
    except Unauthorized:
        return json_error('You are not allowed to access this cohort')
    except NoResultFound:
        return json_error('This cohort does not exist')


def num_users(session, cohort_id):
    user_count = session.query(CohortUserStore) \
        .filter(CohortUserStore.cohort_id == cohort_id) \
        .count()
    return user_count


def get_role(session, cohort_id):
    """
    Returns the role of the current user.
    """
    try:
        cohort_user = session.query(CohortUserStore.role) \
            .filter(CohortUserStore.cohort_id == cohort_id) \
            .filter(CohortUserStore.user_id == current_user.id) \
            .one()[0]
        return cohort_user
    except:
        session.rollback()
        raise DatabaseError('No role found in cohort user.')


def delete_viewer_cohort(session, cohort_id):
    """
    Used when deleting a user's connection to a cohort. Currently used when user
    is a VIEWER of a cohort and want to remove that cohort from their list.

    Raises exception when viewer is duplicated, nonexistent, or can not be deleted.
    """
    cu = session.query(CohortUserStore) \
        .filter(CohortUserStore.cohort_id == cohort_id) \
        .filter(CohortUserStore.user_id == current_user.id) \
        .filter(CohortUserStore.role == CohortUserRole.VIEWER) \
        .delete()

    if cu != 1:
        session.rollback()
        raise DatabaseError('Viewer attempt delete cohort failed.')


def delete_owner_cohort(session, cohort_id):
    """
    Deletes the cohort and all associate records with that cohort if user is the
    owner.

    Raises an error if it cannot delete the cohort.
    """
    # Check that there's only one owner and delete it
    cu = session.query(CohortUserStore) \
        .filter(CohortUserStore.cohort_id == cohort_id) \
        .filter(CohortUserStore.role == CohortUserRole.OWNER) \
        .delete()

    if cu != 1:
        session.rollback()
        raise DatabaseError('No owner or multiple owners in cohort.')
    else:
        try:
            # Delete all other non-owners from cohort_user
            session.query(CohortUserStore) \
                .filter(CohortUserStore.cohort_id == cohort_id) \
                .delete()
            session.query(CohortWikiUserStore) \
                .filter(CohortWikiUserStore.cohort_id == cohort_id) \
                .delete()

            session.query(WikiUserStore) \
                .filter(WikiUserStore.validating_cohort == cohort_id) \
                .delete()

            c = session.query(CohortStore) \
                .filter(CohortStore.id == cohort_id) \
                .delete()
            if c < 1:
                raise DatabaseError
        except DatabaseError:
            session.rollback()
            raise DatabaseError('Owner attempt to delete a cohort failed.')


@app.route('/cohorts/delete/<int:cohort_id>', methods=['POST'])
def delete_cohort(cohort_id):
    """
    Deletes a cohort and all its associated links if it belongs to only current_user
    Removes the relationship between current_user and this cohort if it belongs
    to a user other than current_user
    """
    session = db.get_session()
    try:
        owner_and_viewers = num_users(session, cohort_id)
        role = get_role(session, cohort_id)

        # Owner wants to delete, no other viewers or
        # Owner wants to delete, have other viewers, delete from other viewer's lists too
        if owner_and_viewers >= 1 and role == CohortUserRole.OWNER:
            delete_owner_cohort(session, cohort_id)
            session.commit()
            return json_redirect(url_for('cohorts_index'))

        # Viewer wants to delete cohort from their list, doesn't delete cohort from db;l,
        elif owner_and_viewers > 1 and role == CohortUserRole.VIEWER:
            delete_viewer_cohort(session, cohort_id)
            session.commit()
            return json_redirect(url_for('cohorts_index'))

        # None of the other cases fit.
        else:
            session.rollback()
            return json_error('This Cohort can not be deleted.')
    except DatabaseError as e:
        session.rollback()
        return json_error(e.message)


@app.route('/cohorts/<int:cohort_id>/tag/add/', defaults={'tag': None}, methods=['POST'])
@app.route('/cohorts/<int:cohort_id>/tag/add/<string:tag>', methods=['POST'])
def add_tag(cohort_id, tag):
    """
    Checks if tag exists in the tag table and then adds tag to the cohort.
    """
    if tag is None:
        return json_error(message='You cannot submit an empty tag.')
    parsed_tag = parse_tag(tag)
    session = db.get_session()
    data = {}
    try:
        t = session.query(TagStore).filter(TagStore.name == parsed_tag).first()
        if not t:
            t = TagStore(
                name=parsed_tag
            )
            session.add(t)
            session.commit()

        # Check if cohort is already tagged with 'tag'
        ct = session.query(CohortTagStore) \
            .filter(CohortTagStore.tag_id == t.id) \
            .filter(CohortTagStore.cohort_id == cohort_id) \
            .all()
        if ct:
            return json_response(exists=True)

        cohort_tag = CohortTagStore(
            tag_id=t.id,
            cohort_id=cohort_id,
        )
        session.add(cohort_tag)
        session.commit()
        data['tags'] = populate_cohort_tags(cohort_id, session)

        tagsAutocompleteList = g.tag_service.get_all_tags(session)
        data['tagsAutocompleteList'] = json.dumps(tagsAutocompleteList)

    except DatabaseError as e:
        session.rollback()
        return json_error(e.message)

    return json_response(data)


@app.route('/cohorts/<int:cohort_id>/tag/list')
def cohort_tag_list(cohort_id):
    session = db.get_session()

    # tag_names returns tuples, why?
    tag_names = session.query(TagStore.name) \
        .filter(CohortTagStore.cohort_id == cohort_id) \
        .filter(CohortTagStore.tag_id == TagStore.id) \
        .all()
    tag_names = [tag[0] for tag in tag_names]
    return json.dumps(sorted(tag_names))


@app.route('/cohorts/<int:cohort_id>/tag/delete/<int:tag_id>', methods=['POST'])
def delete_tag(cohort_id, tag_id):
    session = db.get_session()
    session.query(CohortTagStore) \
        .filter(CohortTagStore.cohort_id == cohort_id) \
        .filter(CohortTagStore.tag_id == tag_id) \
        .delete()
    session.commit()

    tags = g.tag_service.get_all_tags(session)
    return json_response(message='success', tagsAutocompleteList=json.dumps(tags))
