// Patches to run Flutter's gen_snapshot (a macOS binary) under Darling.
// Injected with DYLD_INSERT_LIBRARIES by xlinux/core/darling.py.
//
// 1. uname(): Darling reports Darwin 20 (macOS 11) and the Dart VM requires
//    macOS 12+. We report Darwin 21.
// 2. vm_map()/mach_vm_map(): Dart reserves aligned heap pages (mask != 0).
//    Darling doesn't implement that variant and returns an error, Dart runs
//    out of memory and the GC crashes. We emulate it with mmap + trimming.
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/utsname.h>

static int shim_uname(struct utsname *name) {
  int rc = uname(name);
  if (rc == 0 && strncmp(name->release, "20.", 3) == 0) {
    strlcpy(name->release, "21.6.0", sizeof(name->release));
  }
  return rc;
}

static int to_prot(vm_prot_t p) {
  int prot = PROT_NONE;
  if (p & VM_PROT_READ) prot |= PROT_READ;
  if (p & VM_PROT_WRITE) prot |= PROT_WRITE;
  if (p & VM_PROT_EXECUTE) prot |= PROT_EXEC;
  return prot;
}

static kern_return_t shim_mach_vm_map(vm_map_t target, mach_vm_address_t *address,
                                      mach_vm_size_t size, mach_vm_offset_t mask, int flags,
                                      mem_entry_name_port_t object, memory_object_offset_t offset,
                                      boolean_t copy, vm_prot_t cur, vm_prot_t max,
                                      vm_inherit_t inheritance) {
  if (mask == 0 || object != MACH_PORT_NULL || target != mach_task_self() ||
      !(flags & VM_FLAGS_ANYWHERE)) {
    return mach_vm_map(target, address, size, mask, flags, object, offset, copy, cur, max,
                       inheritance);
  }
  uintptr_t align = (uintptr_t)mask + 1;
  size_t padded = size + align;
  void *raw = mmap(NULL, padded, to_prot(cur), MAP_ANON | MAP_PRIVATE, -1, 0);
  if (raw == MAP_FAILED) return KERN_NO_SPACE;
  uintptr_t start = (uintptr_t)raw;
  uintptr_t aligned = (start + mask) & ~(uintptr_t)mask;
  if (aligned > start) munmap(raw, aligned - start);
  uintptr_t end = aligned + size;
  if (start + padded > end) munmap((void *)end, start + padded - end);
  *address = aligned;
  return KERN_SUCCESS;
}

static kern_return_t shim_vm_map(vm_map_t target, vm_address_t *address, vm_size_t size,
                                 vm_address_t mask, int flags, mem_entry_name_port_t object,
                                 vm_offset_t offset, boolean_t copy, vm_prot_t cur, vm_prot_t max,
                                 vm_inherit_t inheritance) {
  mach_vm_address_t addr = *address;
  kern_return_t kr = shim_mach_vm_map(target, &addr, size, mask, flags, object, offset, copy, cur,
                                      max, inheritance);
  if (kr == KERN_SUCCESS) *address = (vm_address_t)addr;
  return kr;
}

__attribute__((used, section("__DATA,__interpose")))
static struct { const void *replacement, *original; } interposers[] = {
  {(const void *)shim_uname, (const void *)uname},
  {(const void *)shim_mach_vm_map, (const void *)mach_vm_map},
  {(const void *)shim_vm_map, (const void *)vm_map},
};
