from apps.staff.roles import StaffRole

VIEW_CONTACT_SUBMISSIONS = "content.view_contactsubmission"

SUPPORT_AGENT = StaffRole(name="Support Agent", permissions=(VIEW_CONTACT_SUBMISSIONS,))

ROLES = (SUPPORT_AGENT,)
