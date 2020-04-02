

import datetime
import logging
from itertools import groupby

import attr
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.db.models import F, Q
from django.urls import reverse
from edx_ace.recipient import Recipient
from edx_ace.recipient_resolver import RecipientResolver
from edx_django_utils.monitoring import function_trace, set_custom_metric

from lms.djangoapps.courseware.utils import verified_upgrade_deadline_link, verified_upgrade_link_is_valid
from lms.djangoapps.discussion.notification_prefs.views import UsernameCipher
from openedx.core.djangoapps.ace_common.template_context import get_base_template_context
from openedx.core.djangoapps.schedules.config import COURSE_UPDATE_SHOW_UNSUBSCRIBE_WAFFLE_SWITCH
from openedx.core.djangoapps.schedules.content_highlights import get_week_highlights, get_next_section_highlights
from openedx.core.djangoapps.schedules.exceptions import CourseUpdateDoesNotExist
from openedx.core.djangoapps.schedules.message_types import CourseUpdate, InstructorLedCourseUpdate
from openedx.core.djangoapps.schedules.models import Schedule, ScheduleExperience
from openedx.core.djangoapps.schedules.utils import PrefixedDebugLoggerMixin
from openedx.core.djangoapps.site_configuration.models import SiteConfiguration
from openedx.core.djangolib.translation_utils import translate_date
from openedx.features.course_experience import course_home_url_name

LOG = logging.getLogger(__name__)

DEFAULT_NUM_BINS = 24
RECURRING_NUDGE_NUM_BINS = DEFAULT_NUM_BINS
UPGRADE_REMINDER_NUM_BINS = DEFAULT_NUM_BINS
COURSE_UPDATE_NUM_BINS = DEFAULT_NUM_BINS


@attr.s
class BinnedSchedulesBaseResolver(PrefixedDebugLoggerMixin, RecipientResolver):
    """
    Identifies learners to send messages to, pulls all needed context and sends a message to each learner.

    Note that for performance reasons, it actually enqueues a task to send the message instead of sending the message
    directly.

    Arguments:
        async_send_task -- celery task function that sends the message
        site -- Site object that filtered Schedules will be a part of
        target_datetime -- datetime that the User's Schedule's schedule_date_field value should fall under
        day_offset -- int number of days relative to the Schedule's schedule_date_field that we are targeting
        bin_num -- int for selecting the bin of Users whose id % num_bins == bin_num
        org_list -- list of course_org names (strings) that the returned Schedules must or must not be in
                    (default: None)
        exclude_orgs -- boolean indicating whether the returned Schedules should exclude (True) the course_orgs in
                        org_list or strictly include (False) them (default: False)
        override_recipient_email -- string email address that should receive all emails instead of the normal
                                    recipient. (default: None)

    Static attributes:
        schedule_date_field -- the name of the model field that represents the date that offsets should be computed
                               relative to. For example, if this resolver finds schedules that started 7 days ago
                               this variable should be set to "start".
        num_bins -- the int number of bins to split the users into
        experience_filter -- a queryset filter used to select only the users who should be getting this message as part
                             of their experience. This defaults to users without a specified experience type and those
                             in the "recurring nudges and upgrade reminder" experience.
    """
    async_send_task = attr.ib()
    site = attr.ib()
    target_datetime = attr.ib()
    day_offset = attr.ib()
    bin_num = attr.ib()
    override_recipient_email = attr.ib(default=None)

    schedule_date_field = None
    num_bins = DEFAULT_NUM_BINS
    experience_filter = (Q(experience__experience_type=ScheduleExperience.EXPERIENCES.default)
                         | Q(experience__isnull=True))

    def __attrs_post_init__(self):
        # TODO: in the next refactor of this task, pass in current_datetime instead of reproducing it here
        self.current_datetime = self.target_datetime - datetime.timedelta(days=self.day_offset)

    def send(self, msg_type):
        for (user, language, context) in self.schedules_for_bin():
            msg = msg_type.personalize(
                Recipient(
                    user.username,
                    self.override_recipient_email or user.email,
                ),
                language,
                context,
            )
            with function_trace('enqueue_send_task'):
                self.async_send_task.apply_async((self.site.id, str(msg)), retry=False)

    @classmethod
    def bin_num_for_user_id(cls, user_id):
        """
        Returns the bin number used for the given (numeric) user ID.
        """
        return user_id % cls.num_bins

    def get_schedules_with_target_date_by_bin_and_orgs(
        self, order_by='enrollment__user__id'
    ):
        """
        Returns Schedules with the target_date, related to Users whose id matches the bin_num, and filtered by org_list.

        Arguments:
        order_by -- string for field to sort the resulting Schedules by
        """
        target_day = _get_datetime_beginning_of_day(self.target_datetime)
        print('self.target_datetime: {}'.format(self.target_datetime))
        print('self.schedule_date_field: {}'.format(self.schedule_date_field))
        schedule_day_equals_target_day_filter = {
            'courseenrollment__schedule__{}__gte'.format(self.schedule_date_field): target_day,
            'courseenrollment__schedule__{}__lt'.format(self.schedule_date_field): target_day + datetime.timedelta(days=1),
        }
        users = User.objects.filter(
            courseenrollment__is_active=True,
            is_active=True,
            **schedule_day_equals_target_day_filter
        ).annotate(
            id_mod=self.bin_num_for_user_id(F('id'))
        ).filter(
            id_mod=self.bin_num
        )
        print('users: {}'.format(users))

        schedule_day_equals_target_day_filter = {
            '{}__gte'.format(self.schedule_date_field): target_day,
            '{}__lt'.format(self.schedule_date_field): target_day + datetime.timedelta(days=1),
        }
        schedules = Schedule.objects.select_related(
            'enrollment__user__profile',
            'enrollment__course',
            'enrollment__fbeenrollmentexclusion',
        ).filter(
            Q(enrollment__course__end__isnull=True) | Q(
                enrollment__course__end__gte=self.current_datetime
            ),
            self.experience_filter,
            enrollment__user__in=users,
            enrollment__is_active=True,
            active=True,
            **schedule_day_equals_target_day_filter
        ).order_by(order_by)

        schedules = self.filter_by_org(schedules)

        if "read_replica" in settings.DATABASES:
            schedules = schedules.using("read_replica")

        #LOG.info(u'Query = %r', schedules.query.sql_with_params())

        with function_trace('schedule_query_set_evaluation'):
            # This will run the query and cache all of the results in memory.
            num_schedules = len(schedules)

        #LOG.info(u'Number of schedules = %d', num_schedules)

        # This should give us a sense of the volume of data being processed by each task.
        set_custom_metric('num_schedules', num_schedules)

        return schedules

    def filter_by_org(self, schedules):
        """
        Given the configuration of sites, get the list of orgs that should be included or excluded from this send.

        Returns:
             tuple: Returns a tuple (exclude_orgs, org_list). If exclude_orgs is True, then org_list is a list of the
                only orgs that should be included in this send. If exclude_orgs is False, then org_list is a list of
                orgs that should be excluded from this send. All other orgs should be included.
        """
        try:
            site_config = self.site.configuration
            org_list = site_config.get_value('course_org_filter')
            if not org_list:
                not_orgs = set()
                for other_site_config in SiteConfiguration.objects.all():
                    other = other_site_config.get_value('course_org_filter')
                    if not isinstance(other, list):
                        if other is not None:
                            not_orgs.add(other)
                    else:
                        not_orgs.update(other)
                return schedules.exclude(enrollment__course__org__in=not_orgs)
            elif not isinstance(org_list, list):
                return schedules.filter(enrollment__course__org=org_list)
        except SiteConfiguration.DoesNotExist:
            return schedules

        return schedules.filter(enrollment__course__org__in=org_list)

    def schedules_for_bin(self):
        schedules = self.get_schedules_with_target_date_by_bin_and_orgs()
        template_context = get_base_template_context(self.site)

        for (user, user_schedules) in groupby(schedules, lambda s: s.enrollment.user):
            user_schedules = list(user_schedules)
            course_id_strs = [str(schedule.enrollment.course_id) for schedule in user_schedules]

            # This is used by the bulk email optout policy
            template_context['course_ids'] = course_id_strs

            first_schedule = user_schedules[0]
            try:
                template_context.update(self.get_template_context(user, user_schedules))
            except InvalidContextError:
                continue

            yield (user, first_schedule.enrollment.course.closest_released_language, template_context)

    def get_template_context(self, user, user_schedules):
        """
        Given a user and their schedules, build the context needed to render the template for this message.

        Arguments:
             user -- the User who will be receiving the message
             user_schedules -- a list of Schedule objects representing all of their schedules that should be covered by
                               this message. For example, when a user enrolls in multiple courses on the same day, we
                               don't want to send them multiple reminder emails. Instead this list would have multiple
                               elements, allowing us to send a single message for all of the courses.

        Returns:
            dict: This dict must be JSON serializable (no datetime objects!). When rendering the message templates it
                  it will be used as the template context. Note that it will also include several default values that
                  injected into all template contexts. See `get_base_template_context` for more information.

        Raises:
            InvalidContextError: If this user and set of schedules are not valid for this type of message. Raising this
            exception will prevent this user from receiving the message, but allow other messages to be sent to other
            users.
        """
        return {}


class InvalidContextError(Exception):
    pass


class RecurringNudgeResolver(BinnedSchedulesBaseResolver):
    """
    Send a message to all users whose schedule started at ``self.current_date`` + ``day_offset``.
    """
    log_prefix = 'Recurring Nudge'
    schedule_date_field = 'start_date'
    num_bins = RECURRING_NUDGE_NUM_BINS

    @property
    def experience_filter(self):
        if self.day_offset == -3:
            experiences = [ScheduleExperience.EXPERIENCES.default, ScheduleExperience.EXPERIENCES.course_updates]
            return Q(experience__experience_type__in=experiences) | Q(experience__isnull=True)
        else:
            return Q(experience__experience_type=ScheduleExperience.EXPERIENCES.default) | Q(experience__isnull=True)

    def get_template_context(self, user, user_schedules):
        first_schedule = user_schedules[0]
        if not first_schedule.enrollment.course.self_paced:
            raise InvalidContextError
        context = {
            'course_name': first_schedule.enrollment.course.display_name,
            'course_url': _get_trackable_course_home_url(first_schedule.enrollment.course_id),
        }

        # Information for including upsell messaging in template.
        context.update(_get_upsell_information_for_schedule(user, first_schedule))

        return context


def _get_datetime_beginning_of_day(dt):
    """
    Truncates hours, minutes, seconds, and microseconds to zero on given datetime.
    """
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


class UpgradeReminderResolver(BinnedSchedulesBaseResolver):
    """
    Send a message to all users whose verified upgrade deadline is at ``self.current_date`` + ``day_offset``.
    """
    log_prefix = 'Upgrade Reminder'
    schedule_date_field = 'upgrade_deadline'
    num_bins = UPGRADE_REMINDER_NUM_BINS

    def get_template_context(self, user, user_schedules):
        course_id_strs = []
        course_links = []
        first_valid_upsell_context = None
        first_schedule = None
        for schedule in user_schedules:
            if not schedule.enrollment.course.self_paced:
                # We don't want to include instructor led courses in this email
                continue

            upsell_context = _get_upsell_information_for_schedule(user, schedule)
            if not upsell_context['show_upsell']:
                continue

            if first_valid_upsell_context is None:
                first_schedule = schedule
                first_valid_upsell_context = upsell_context
            course_id_str = str(schedule.enrollment.course_id)
            course_id_strs.append(course_id_str)
            course_links.append({
                'url': _get_trackable_course_home_url(schedule.enrollment.course_id),
                'name': schedule.enrollment.course.display_name
            })

        if first_schedule is None:
            self.log_debug('No courses eligible for upgrade for user.')
            raise InvalidContextError()

        context = {
            'course_links': course_links,
            'first_course_name': first_schedule.enrollment.course.display_name,
            'cert_image': static('course_experience/images/verified-cert.png'),
            'course_ids': course_id_strs,
        }
        context.update(first_valid_upsell_context)
        return context


def _get_upsell_information_for_schedule(user, schedule):
    template_context = {}
    enrollment = schedule.enrollment
    course = enrollment.course

    verified_upgrade_link = _get_verified_upgrade_link(user, schedule)
    has_verified_upgrade_link = verified_upgrade_link is not None

    if has_verified_upgrade_link:
        template_context['upsell_link'] = verified_upgrade_link
        template_context['user_schedule_upgrade_deadline_time'] = translate_date(
            date=enrollment.dynamic_upgrade_deadline,
            language=course.closest_released_language,
        )

    template_context['show_upsell'] = has_verified_upgrade_link
    return template_context


def _get_verified_upgrade_link(user, schedule):
    enrollment = schedule.enrollment
    if enrollment.dynamic_upgrade_deadline is not None and verified_upgrade_link_is_valid(enrollment):
        return verified_upgrade_deadline_link(user, enrollment.course)


class CourseUpdateResolver(BinnedSchedulesBaseResolver):
    """
    Send a message to all users whose schedule started at ``self.current_date`` + ``day_offset`` and the
    course has updates.
    """
    log_prefix = 'Course Update'
    schedule_date_field = 'start_date'
    num_bins = COURSE_UPDATE_NUM_BINS
    experience_filter = Q(experience__experience_type=ScheduleExperience.EXPERIENCES.course_updates)

    def send(self, msg_type):
        for (user, language, context, is_self_paced) in self.schedules_for_bin():
            msg_type = CourseUpdate() if is_self_paced else InstructorLedCourseUpdate()
            msg = msg_type.personalize(
                Recipient(
                    user.username,
                    self.override_recipient_email or user.email,
                ),
                language,
                context,
            )
            with function_trace('enqueue_send_task'):
                self.async_send_task.apply_async((self.site.id, str(msg)), retry=False)  # pylint: disable=no-member

    def schedules_for_bin(self):
        week_num = abs(self.day_offset) // 7
        schedules = self.get_schedules_with_target_date_by_bin_and_orgs(
            order_by='enrollment__course',
        )

        template_context = get_base_template_context(self.site)
        for schedule in schedules:
            enrollment = schedule.enrollment
            course = schedule.enrollment.course
            user = enrollment.user

            try:
                # week_highlights = get_week_highlights(user, enrollment.course_id, week_num)
                # TODO: Uncomment below and remove above line when enabling AA-68
                week_highlights = get_next_section_highlights(user, enrollment.course_id)
            except CourseUpdateDoesNotExist:
                LOG.warning(
                    u'Weekly highlights for user {} in week {} of course {} does not exist or is disabled'.format(
                        user, week_num, enrollment.course_id
                    )
                )
                # continue to the next schedule, don't yield an email for this one
            else:
                unsubscribe_url = None
                if (COURSE_UPDATE_SHOW_UNSUBSCRIBE_WAFFLE_SWITCH.is_enabled() and
                        'bulk_email_optout' in settings.ACE_ENABLED_POLICIES):
                    unsubscribe_url = reverse('bulk_email_opt_out', kwargs={
                        'token': UsernameCipher.encrypt(user.username),
                        'course_id': str(enrollment.course_id),
                    })

                print('Jeff 9')
                template_context.update({
                    'course_name': schedule.enrollment.course.display_name,
                    'course_url': _get_trackable_course_home_url(enrollment.course_id),

                    'week_num': week_num,
                    'week_highlights': week_highlights,

                    # This is used by the bulk email optout policy
                    'course_ids': [str(enrollment.course_id)],
                    'unsubscribe_url': unsubscribe_url,
                })
                template_context.update(_get_upsell_information_for_schedule(user, schedule))

                print('Jeff 10')
                yield (user, schedule.enrollment.course.closest_released_language, template_context, course.self_paced)


def _get_trackable_course_home_url(course_id):
    """
    Get the home page URL for the course.

    NOTE: For us to be able to track clicks in the email, this URL needs to point to a landing page that does not result
    in a redirect so that the GA snippet can register the UTM parameters.

    Args:
        course_id (CourseKey): The course to get the home page URL for.

    Returns:
        A relative path to the course home page.
    """
    course_url_name = course_home_url_name(course_id)
    return reverse(course_url_name, args=[str(course_id)])
