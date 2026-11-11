"""Learning roles.

Three jobs, deliberately separated. Writing content and releasing it: an editor can draft and
change anything; only a publisher can put it in front of students, so a mistake in a draft
cannot reach anybody because the wrong person pressed save.

Answering the people who ask for a free chapter is the third, and it is nobody's job by default.
Those rows are visitors' email addresses and their marketing consent, which writing an e-book
never needs to see, so they are granted on their own and to whoever actually answers that inbox.
"""

from apps.staff.roles import StaffRole

CONTENT_MODELS = (
    "chapter",
    "subchapter",
    "learningcontent",
    "sign",
    "mediaasset",
    "subtitle",
    "hazardclip",
    "hazardwindow",
    "question",
    "questionoption",
    "practiceexam",
    "examquestion",
    "examhazardclip",
    "ebook",
    "ebooksection",
)

# What students have done. Looked at, never edited, so only the view permission is granted —
# and a role that could change these could rewrite somebody's history.
PROGRESS_MODELS = (
    "questionattempt",
    "mockattempt",
    "lessonprogress",
    "videoprogress",
    "savedquestion",
    "hazardattempt",
    "ebooksectionprogress",
)
WATCHING = tuple(f"learning.view_{model}" for model in PROGRESS_MODELS)
LEADS = ("learning.view_ebooksamplerequest",)

# Everything needed to write and change learning content, but not to release it.
EDITING = tuple(f"learning.{action}_{model}" for model in CONTENT_MODELS for action in ("view", "add", "change"))
INLINE_MODELS = (
    "subtitle",
    "hazardwindow",
    "questionoption",
    "examquestion",
    "examhazardclip",
)
REMOVING = tuple(f"learning.delete_{model}" for model in INLINE_MODELS)
PUBLISHABLE = (
    "chapter",
    "subchapter",
    "learningcontent",
    "question",
    "practiceexam",
    "hazardclip",
    "ebook",
    "ebooksection",
)
PUBLISHING = tuple(f"learning.publish_{model}" for model in PUBLISHABLE)

CONTENT_EDITOR = StaffRole(name="Content Editor", permissions=EDITING + REMOVING + WATCHING)
CONTENT_PUBLISHER = StaffRole(name="Content Publisher", permissions=EDITING + REMOVING + PUBLISHING + WATCHING)
MARKETING_SUPPORT = StaffRole(name="Marketing Support", permissions=LEADS)

ROLES = (CONTENT_EDITOR, CONTENT_PUBLISHER, MARKETING_SUPPORT)
