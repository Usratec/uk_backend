from django.urls import path

from apps.learning import api

urlpatterns = [
    # The frontend's own words: a chapter is a "topic" and a subchapter is a "lesson".
    path("learn/topics/", api.TopicListView.as_view(), name="learn-topics"),
    path("learn/lessons/<slug:slug>/", api.LessonDetailView.as_view(), name="learn-lesson"),
    path("learn/ebook/", api.EbookView.as_view(), name="learn-ebook"),
    path("learn/ebook/<slug:slug>/", api.EbookChapterView.as_view(), name="learn-ebook-chapter"),
    path("learn/signs/", api.SignListView.as_view(), name="learn-signs"),
    path("learn/practice/", api.PracticeSetView.as_view(), name="learn-practice"),
    path("learn/answer/", api.AnswerView.as_view(), name="learn-answer"),
    path("learn/exams/", api.ExamListView.as_view(), name="learn-exams"),
    path("learn/exams/<slug:slug>/", api.ExamDetailView.as_view(), name="learn-exam"),
    path("learn/exams/<slug:slug>/submit/", api.ExamSubmitView.as_view(), name="learn-exam-submit"),
    # A student's own sittings: the history of one exam, and the saved results of one attempt.
    path("learn/exams/<slug:slug>/attempts/", api.ExamAttemptListView.as_view(), name="learn-exam-attempts"),
    path("learn/exam-attempts/<int:attempt_id>/", api.ExamAttemptView.as_view(), name="learn-exam-attempt"),
    # What the student has done. Recorded by the endpoints above where it can be, and by these
    # where only the student can say so.
    path("learn/lessons/<slug:slug>/complete/", api.LessonCompleteView.as_view(), name="learn-lesson-complete"),
    path("learn/questions/<slug:key>/saved/", api.SavedQuestionView.as_view(), name="learn-question-saved"),
    path("learn/progress/", api.ProgressView.as_view(), name="learn-progress"),
    # Hazard perception. The clip's hazard timings are never sent, only its score afterwards.
    path("learn/hazard/", api.HazardClipListView.as_view(), name="learn-hazard-clips"),
    path("learn/hazard/<slug:slug>/", api.HazardClipView.as_view(), name="learn-hazard-clip"),
    path("learn/hazard/<slug:slug>/attempt/", api.HazardAttemptView.as_view(), name="learn-hazard-attempt"),
]
