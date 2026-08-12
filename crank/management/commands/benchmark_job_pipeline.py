# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Run the offline synthetic job-pipeline benchmark."""

from django.core.management.base import BaseCommand, CommandError

from crank.services.job_pipeline_benchmark import (
    get_profile,
    metrics_json,
    run_benchmark,
)


class Command(BaseCommand):
    help = "Run the deterministic, offline job-pipeline benchmark."

    def add_arguments(self, parser):
        parser.add_argument(
            "--profile",
            choices=("ci", "staging"),
            default="ci",
            help="Synthetic workload profile (default: ci).",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=324,
            help="Non-negative synthetic-data seed (default: 324).",
        )
        parser.add_argument(
            "--assert-budgets",
            action="store_true",
            help="Exit non-zero when a profile budget is exceeded.",
        )

    def handle(self, *args, **options):
        try:
            get_profile(options["profile"])
            metrics = run_benchmark(
                options["profile"],
                seed=options["seed"],
                assert_budgets=options["assert_budgets"],
            )
        except (AssertionError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(metrics_json(metrics))
        return 0
