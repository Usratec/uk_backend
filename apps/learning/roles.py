"""Learning roles.

Two jobs, deliberately separated: writing content and releasing it. An editor can draft and
change anything; only a publisher can put it in front of students. That way a mistake in a draft
cannot reach anybody because the wrong person pressed save.
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

# Everything needed to write and change learning content, but not to release it.
EDITING = tuple(f"learning.{action}_{model}" for model in CONTENT_MODELS for action in ("view", "add", "change"))
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

CONTENT_EDITOR = StaffRole(name="Content Editor", permissions=EDITING + WATCHING)
CONTENT_PUBLISHER = StaffRole(name="Content Publisher", permissions=EDITING + PUBLISHING + WATCHING)

ROLES = (CONTENT_EDITOR, CONTENT_PUBLISHER)
