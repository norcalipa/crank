// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
import * as React from 'react';
import SuggestCompanyModal from '../SuggestCompanyModal';
import {closeSuggestCompany, getSuggestCompanySnapshot, subscribeSuggestCompany} from './controller';

// The single mounted instance of the shared "Suggest a company" dialog
// (issue #471). Subscribes to the module-level controller store and renders
// the modal with whatever context the latest trigger provided.
const SuggestCompanyHost: React.FC = () => {
    const state = React.useSyncExternalStore(
        subscribeSuggestCompany, getSuggestCompanySnapshot, getSuggestCompanySnapshot
    );

    return (
        <SuggestCompanyModal
            visible={state.open}
            context={state.context}
            onClose={closeSuggestCompany}
        />
    );
};

export default SuggestCompanyHost;
