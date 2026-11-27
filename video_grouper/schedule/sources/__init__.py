"""Schedule-source implementations, one module each.

Every module here is imported by
``video_grouper.schedule.base.registered_schedule_sources``; a module adds its
source by decorating an ``IScheduleSource`` subclass with
``@register_schedule_source``. To add a source, add a module — nothing else.
"""
