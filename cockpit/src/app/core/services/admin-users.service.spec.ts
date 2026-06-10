import {describe, expect, it, vi} from 'vitest';
import {Injector, runInInjectionContext} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {of} from 'rxjs';
import {AdminUsersService} from './admin-users.service';
import {User} from '../models/api.model';

function createService(mockHttp?: any) {
  const http = mockHttp ?? {
    get: vi.fn().mockReturnValue(of([])),
    put: vi.fn().mockReturnValue(of({})),
    post: vi.fn().mockReturnValue(of({})),
    patch: vi.fn().mockReturnValue(of({})),
    delete: vi.fn().mockReturnValue(of({})),
  };

  const injector = Injector.create({
    providers: [{provide: HttpClient, useValue: http}],
  });

  const service = runInInjectionContext(injector, () => new AdminUsersService());
  return {service, http};
}

describe('AdminUsersService', () => {
  it('loads users (incl. approval status) from /admin/users', () => {
    const rows: Partial<User>[] = [
      {id: '1', display_name: 'A', is_approved: true},
      {id: '2', display_name: 'B', is_approved: false},
    ];
    const http = {
      get: vi.fn().mockReturnValue(of(rows)),
      put: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
    };
    const {service} = createService(http);
    service.loadUsers();
    expect(http.get).toHaveBeenCalledWith(
      expect.stringContaining('/admin/users'),
    );
    expect(service.users()).toHaveLength(2);
    expect(service.users()[1].is_approved).toBe(false);
  });

  it('PATCHes is_approved (suspension) and refreshes the list', () => {
    const http = {
      get: vi.fn().mockReturnValue(of([])),
      patch: vi.fn().mockReturnValue(of({status: 'updated'})),
      put: vi.fn(),
      post: vi.fn(),
      delete: vi.fn(),
    };
    const {service} = createService(http);
    service.patchUser('u-1', {is_approved: false}).subscribe();
    expect(http.patch).toHaveBeenCalledWith(
      expect.stringContaining('/admin/users/u-1'),
      {is_approved: false},
    );
    expect(http.get).toHaveBeenCalled(); // tap reloads
  });

  it('POSTs bulk approval to /admin/users/approve with user_ids and reloads', () => {
    const http = {
      get: vi.fn().mockReturnValue(of([])),
      post: vi.fn().mockReturnValue(
        of({
          approved_count: 2,
          results: [
            {id: 'a', status: 'approved'},
            {id: 'b', status: 'approved'},
          ],
        }),
      ),
      put: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
    };
    const {service} = createService(http);
    let got: any = null;
    service.approveUsers(['a', 'b']).subscribe((r) => (got = r));
    expect(http.post).toHaveBeenCalledWith(
      expect.stringContaining('/admin/users/approve'),
      {user_ids: ['a', 'b']},
    );
    expect(got.approved_count).toBe(2);
    expect(http.get).toHaveBeenCalled(); // tap reloads the list
  });
});
