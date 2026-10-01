// Copyright (c) 2024 Isaac Adams
// Licensed under the MIT License. See LICENSE file in the project root for full license information.
//
// Subscribes a priorities component to the cross-root workspace store.

import * as React from 'react';
import {getWorkspaceSnapshot, subscribeWorkspace} from '../workspace/store';
import type {WorkspaceSnapshot} from '../workspace/types';

export function useWorkspace(): WorkspaceSnapshot {
    return React.useSyncExternalStore(subscribeWorkspace, getWorkspaceSnapshot, getWorkspaceSnapshot);
}
