from django.urls import path

from apps.learning import api, editor_views, media_views

urlpatterns = [
    path("learning/media/upload/", editor_views.media_upload, name="learning-media-upload"),
    path("learning/media/<int:pk>/", editor_views.media_file, name="learning-media-file"),
    # Direct browser-to-bucket video uploads. Staff only: the video bytes never pass through here,
    # only the permission to write one object and the check on what arrived.
    path("learning/media/videos/", media_views.VideoUploadStartView.as_view(), name="learning-video-upload-start"),
    path(
        "learning/media/videos/<uuid:media_id>/",
        media_views.VideoStatusView.as_view(),
        name="learning-video-status",
    ),
    path(
        "learning/media/videos/<uuid:media_id>/parts/",
        media_views.VideoUploadPartsView.as_view(),
        name="learning-video-upload-parts",
    ),
    path(
        "learning/media/videos/<uuid:media_id>/complete/",
        media_views.VideoUploadCompleteView.as_view(),
        name="learning-video-upload-complete",
    ),
    path(
        "learning/media/videos/<uuid:media_id>/cancel/",
        media_views.VideoUploadCancelView.as_view(),
        name="learning-video-upload-cancel",
    ),
    # What a learner asks for when they press play.
    path(
        "learn/media/<uuid:media_id>/playback/",
        media_views.MediaPlaybackView.as_view(),
        name="learn-media-playback",
    ),
    # The frontend's own words: a chapter is a "topic" and a subchapter is a "lesson".
    path("learn/topics/", api.TopicListView.as_view(), name="learn-topics"),
    path("learn/lessons/<slug:slug>/", api.LessonDetailView.as_view(), name="learn-lesson"),
    path("learn/ebook/", api.EbookView.as_view(), name="learn-ebook"),
    path("learn/ebook/<slug:slug>/", api.EbookDetailView.as_view(), name="learn-ebook-detail"),
    path("learn/ebook/<slug:slug>/read/", api.EbookSectionReadView.as_view(), name="learn-ebook-section-read"),
    path("learn/signs/", api.SignListView.as_view(), name="learn-signs"),
    path("learn/practice/", api.PracticeSetView.as_view(), name="learn-practice"),
    path("learn/answer/", api.AnswerView.as_view(), name="learn-answer"),
    path("learn/exams/", api.ExamListView.as_view(), name="learn-exams"),
    path("learn/exams/<slug:slug>/", api.ExamDetailView.as_view(), name="learn-exam"),
    path("learn/exams/<slug:slug>/submit/", api.ExamSubmitView.as_view(), name="learn-exam-submit"),
    path("learn/exam-attempts/", api.AllExamAttemptsView.as_view(), name="learn-exam-attempts-all"),
    path("learn/exams/<slug:slug>/attempts/", api.ExamAttemptListView.as_view(), name="learn-exam-attempts"),
    path("learn/exam-attempts/<int:attempt_id>/", api.ExamAttemptView.as_view(), name="learn-exam-attempt"),
    # What the student has done. Recorded by the endpoints above where it can be, and by these
    # where only the student can say so.
    path("learn/lessons/<slug:slug>/complete/", api.LessonCompleteView.as_view(), name="learn-lesson-complete"),
    path("learn/videos/<slug:slug>/progress/", api.VideoProgressView.as_view(), name="learn-video-progress"),
    path("learn/questions/<slug:key>/saved/", api.SavedQuestionView.as_view(), name="learn-question-saved"),
    path("learn/progress/", api.ProgressView.as_view(), name="learn-progress"),
    # Hazard perception. The clip's hazard timings are never sent, only its score afterwards.
    path("learn/hazard/", api.HazardClipListView.as_view(), name="learn-hazard-clips"),
    path("learn/hazard/<slug:slug>/", api.HazardClipView.as_view(), name="learn-hazard-clip"),
    path("learn/hazard/<slug:slug>/attempt/", api.HazardAttemptView.as_view(), name="learn-hazard-attempt"),
]
