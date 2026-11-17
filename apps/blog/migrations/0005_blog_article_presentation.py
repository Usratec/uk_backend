import apps.core.fields
from django.db import migrations, models

from apps.blog import html


def work_out_reading_times(apps, schema_editor):
    post_model = apps.get_model("blog", "BlogPost")
    for post in post_model.objects.exclude(body_html="").only("pk", "body_html").iterator():
        post_model.objects.filter(pk=post.pk).update(reading_minutes=html.minutes_to_read(post.body_html))


class Migration(migrations.Migration):
    dependencies = [
        ('blog', '0004_blog_seo_help_text'),
    ]

    operations = [
        migrations.AddField(
            model_name='blogpost',
            name='cover_image',
            field=apps.core.fields.SanitizedImageField(blank=True, height_field='cover_image_height', help_text='Shown on the cards and at the top of the post. JPG, PNG, WebP or GIF, under 20 MB.', upload_to='blog/covers/', width_field='cover_image_width'),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='cover_image_alt',
            field=models.CharField(blank=True, help_text='What the cover shows, read aloud to anyone who cannot see it. Required with a cover.', max_length=200, verbose_name='cover description'),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='cover_image_height',
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='cover_image_width',
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='excerpt',
            field=models.CharField(blank=True, help_text='The short summary readers see on the blog cards and under the title. Separate from the meta description, which is written for search engines.', max_length=300, verbose_name='summary'),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='featured',
            field=models.BooleanField(db_index=True, default=False, help_text='Featured posts head the blog as the big Featured read.'),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='reading_minutes',
            field=models.PositiveSmallIntegerField(default=0, editable=False, help_text='Worked out from the content every time the post is saved.', verbose_name='reading time'),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='study_tip_button_label',
            field=models.CharField(blank=True, help_text="Optional. The words on the tip's button, e.g. Start a mock test.", max_length=60),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='study_tip_button_link',
            field=models.URLField(blank=True, help_text="Optional. Where the tip's button goes. Needs a label.", max_length=500),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='study_tip_text',
            field=models.TextField(blank=True, max_length=600, verbose_name='study tip'),
        ),
        migrations.AddField(
            model_name='blogpost',
            name='study_tip_title',
            field=models.CharField(blank=True, help_text='Optional. Fill the title and the tip to show a Study tip box under the post.', max_length=120),
        ),
        migrations.RunPython(work_out_reading_times, migrations.RunPython.noop),
    ]
