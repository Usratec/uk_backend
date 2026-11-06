from apps.staff.roles import StaffRole

BLOG_MODELS = ("blogpost", "blogcategory", "blogimage", "blogpostredirect")

MANAGING = tuple(f"blog.{action}_{model}" for model in BLOG_MODELS for action in ("view", "add", "change", "delete"))
PUBLISHING = ("blog.publish_blogpost",)

BLOG_MANAGER = StaffRole(name="Blog Manager", permissions=MANAGING + PUBLISHING)

ROLES = (BLOG_MANAGER,)
