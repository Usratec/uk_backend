from django_ckeditor_5.widgets import CKEditor5Widget  # type: ignore[import-untyped]  # ships no types


class BlogCKEditor5Widget(CKEditor5Widget):
    template_name = "blog/ckeditor5_widget.html"
