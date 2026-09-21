// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// The `main` webpack entry (issue #471). Loaded in <head> on every page
// (templates/base.html), so this is the one bundle guaranteed to run
// everywhere — the reason the shared "Suggest a company" host lives here
// rather than in a dedicated bundle (see suggestCompany/controller.ts).
import './OrganizationList';
import './suggestCompany/mount';
import './workspace/mount';
