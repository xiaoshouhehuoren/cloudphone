#include <base/logging.h>
void f() {
  LOG(INFO) << "hello" << 1;
  LOG_IF(ERROR, 1 == 2) << "nope";
  VLOG(1) << "verbose";
}
