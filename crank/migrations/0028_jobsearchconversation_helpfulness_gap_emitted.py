# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Add a durable one-time emission marker to JobSearchConversation.

Issue #423 MINOR-2: ``assistant_turns == MIN_HELPFUL_TURNS`` was only a derived
snapshot, so concurrent submissions could double-emit (or miss) the
``job_search_helpfulness_gap`` telemetry event. This durable boolean is flipped
false -> true atomically (a conditional update) so the one-time signal is
emitted exactly once even under concurrency.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crank", "0027_jobretrievalops"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobsearchconversation",
            name="helpfulness_gap_emitted",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True once the one-time job_search_helpfulness_gap "
                    "telemetry event has been emitted for this conversation. "
                    "Set atomically so concurrent submissions emit the gap "
                    "signal exactly once."
                ),
            ),
        ),
    ]
