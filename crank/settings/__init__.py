# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
from .base import *

# you need to set "myproject = 'prod'" as an environment variable
# in your OS (on which your website is hosted)
if os.environ['ENV'] == 'prod':
    from .prod import *
elif os.environ['ENV'] == 'staging':
    from .staging import *
else:
    from .dev import *
