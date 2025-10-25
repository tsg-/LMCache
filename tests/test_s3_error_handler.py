#!/usr/bin/env python3
"""Test that S3 error handler exits immediately on upload failures."""

import logging
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.hpu_selective_benchmark import S3UploadErrorHandler


def test_s3_error_detection():
    """Verify that S3 upload errors trigger immediate exit."""
    
    # Create a logger with our custom handler
    logger = logging.getLogger("lmcache.test")
    logger.setLevel(logging.DEBUG)
    
    handler = S3UploadErrorHandler()
    handler.setLevel(logging.ERROR)
    logger.addHandler(handler)
    
    print("Testing S3 error handler...")
    print("1. Testing non-S3 error (should NOT exit):")
    logger.error("Some random error message")
    print("   ✓ Passed - did not exit")
    
    print("2. Testing partial S3 error (should NOT exit):")
    logger.error("Failed to upload something")
    print("   ✓ Passed - did not exit")
    
    print("3. Testing S3 upload error (SHOULD exit with code 1):")
    print("   This should trigger immediate exit...")
    logger.error(
        "Failed to upload KV_2LTD@dummy-hpu-model@1@0@57031c26e7eb7c0e to S3: "
        "AWS_ERROR_S3_INVALID_RESPONSE_STATUS: Invalid response status from request"
    )
    
    # Should never reach here
    print("   ✗ FAILED - did not exit!")
    sys.exit(1)


if __name__ == "__main__":
    test_s3_error_detection()
