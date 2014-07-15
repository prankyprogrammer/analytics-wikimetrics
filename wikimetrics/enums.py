class Aggregation(object):
    IND = 'Individual Results'
    SUM = 'Sum'
    AVG = 'Average'
    STD = 'Standard Deviation'


class CohortUserRole(object):
    OWNER = 'OWNER'
    VIEWER = 'VIEWER'
    SAFE_ROLES = [OWNER, VIEWER]


class UserRole(object):
    ADMIN = 'ADMIN'
    USER = 'USER'
    GUEST = 'GUEST'
