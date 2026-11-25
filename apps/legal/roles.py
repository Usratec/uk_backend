from apps.staff.roles import StaffRole

MANAGING = tuple(f"legal.{action}_legaldocument" for action in ("view", "add", "change"))
PUBLISHING = ("legal.publish_legaldocument",)

LEGAL_MANAGER = StaffRole(name="Legal Manager", permissions=MANAGING + PUBLISHING)

ROLES = (LEGAL_MANAGER,)
