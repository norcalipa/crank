from django.contrib import admin
from .models import OrganizationType
from .models import Organization
from .models import ScoreType
from .models import ScoreAlgorithm
from .models import ScoreAlgorithmWeight
from .models import Score

admin.site.register(OrganizationType)
admin.site.register(Organization)
admin.site.register(ScoreType)
admin.site.register(ScoreAlgorithm)
admin.site.register(ScoreAlgorithmWeight)
admin.site.register(Score)
