/* Shared Use License: This file is owned by Derivative Inc. (Derivative)
* and can only be used, and/or modified for use, in conjunction with
* Derivative's TouchDesigner software, and only if you are a licensee who has
* accepted Derivative's TouchDesigner license or assignment agreement
* (which also govern the use of this file). You may share or redistribute
* a modified version of this file provided the following conditions are met:
*
* 1. The shared file or redistribution must retain the information set out
* above and this list of conditions.
* 2. Derivative's name (Derivative Inc.) or its trademarks may not be used
* to endorse or promote products derived from this file without specific
* prior written permission from Derivative.
*/

// CUDA kernels for CudaPOP. shiftZPosition() is called by CudaPOP::execute()
// between beginCUDAOperations() and endCUDAOperations().

#include "cuda_runtime.h"
#include "device_launch_parameters.h"

#include <algorithm>
#include <stdio.h>

#include "POP_CPlusPlusBase.h"

using namespace TD;

// shiftZImpl - shared device body: one thread per point adds 1.0 to pos.z.
__device__ void
shiftZImpl(void* buffer, uint32_t numPoints)
{
	unsigned int x = blockIdx.x * blockDim.x + threadIdx.x;

	if (x >= numPoints)
		return;

	float3* pos = (float3*)buffer;
	pos[x].z += 1.0f;
}

// shiftZ - in-place kernel overload taking an explicit point count (CPU-side
// pointInfo location).
__global__ void
shiftZ(void* buffer, uint32_t numPoints)
{
	shiftZImpl(buffer, numPoints);
}

// shiftZ - in-place kernel overload reading numPoints from a GPU-resident
// POP_PointInfo buffer.
__global__ void
shiftZ(void* buffer, POP_PointInfo* pointInfo)
{
	shiftZImpl(buffer, pointInfo->numPoints);
}

// copyAndShiftZImpl - shared device body: copy each point from src to dst and
// add 1.0 to dst.z (used by the COPY_EXAMPLE path in CudaPOP::execute).
__device__ void
copyAndShiftZImpl(void* src, void* dst, uint32_t numPoints)
{
	unsigned int x = blockIdx.x * blockDim.x + threadIdx.x;

	if (x >= numPoints)
		return;

	float3* srcPos = (float3*)src;
	float3* dstPos = (float3*)dst;
	dstPos[x] = srcPos[x];
	dstPos[x].z += 1.0f;
}

// copyAndShiftZ - copy+shift kernel taking an explicit point count.
__global__ void
copyAndShiftZ(void* src, void* dst, uint32_t numPoints)
{
	copyAndShiftZImpl(src, dst, numPoints);
}

// copyAndShiftZ - copy+shift kernel reading numPoints from GPU pointInfo.
__global__ void
copyAndShiftZ(void* src, void* dst, POP_PointInfo *pointInfo)
{
	copyAndShiftZImpl(src, dst, pointInfo->numPoints);
}

int
divUp(int a, int b)
{
	return ((a % b) != 0) ? (a / b + 1) : (a / b);
}

// Host entry point. Adds 1 to P.z of every point, in place or into newDst, on
// 'stream'. The point count comes from pointInfo when it is on the CPU, else the
// launch covers maxInfo.points and the kernel reads the real count on the GPU.
// Returns the launch status (and, in Debug builds, the status after the kernel ran).
cudaError_t
shiftZPosition(const OP_SmartRef<POP_Buffer>& posBuffer, void* newDst, const OP_SmartRef<POP_Buffer>& pointInfo, const POP_MaxInfo& maxInfo, cudaStream_t stream)
{
	cudaError_t cudaStatus;

	// Nothing to do without both buffers (e.g. getAllInfoBuffers() failed).
	if (!posBuffer || !pointInfo)
		return cudaSuccess;

	// We need to determine the size of the compute operation to execute, to ensure we run a thread for each point.
	uint32_t numPoints = 0;
	// If the number of points is known on the CPU, use that value directly.
	if (pointInfo->info.location == POP_BufferLocation::CPU)
	{
		POP_PointInfo* pi = (POP_PointInfo*)(pointInfo->getData(nullptr));
		numPoints = pi->numPoints;
	}
	else
	{
		// Otherwise we need to dispatch up to the max, we'll use the data that's on the GPU to
		// early-exit the threads that arn't needed though.
		numPoints = maxInfo.points;
	}
	if (!numPoints)
		return cudaSuccess;

	dim3 blockSize(32, 1, 1);
	dim3 gridSize(divUp(numPoints, blockSize.x), 1, 1);

	void* origBuffer = posBuffer->getData(nullptr);
	if (pointInfo->info.location == POP_BufferLocation::CPU)
	{
		if (newDst)
			copyAndShiftZ<<<gridSize, blockSize, 0, stream>>>(origBuffer, newDst, numPoints);
		else
			shiftZ<<<gridSize, blockSize, 0, stream>>>(origBuffer, numPoints);
	}
	else
	{
		// These will get the actual numPoints from the pointInfo buffer, which resides on the GPU.
		if (newDst)
			copyAndShiftZ<<<gridSize, blockSize, 0, stream>>>(origBuffer, newDst, (POP_PointInfo*)(pointInfo->getData(nullptr)));
		else
			shiftZ<<<gridSize, blockSize, 0, stream>>>(origBuffer, (POP_PointInfo*)(pointInfo->getData(nullptr)));
	}
	// Launch errors (invalid configuration, bad surface...) are available
	// without synchronizing, so check them in every build and return them to
	// the caller (the official sample only checked in Debug builds).
	cudaStatus = cudaGetLastError();
	if (cudaStatus != cudaSuccess)
	{
		fprintf(stderr, "Kernel launch failed: %s\n", cudaGetErrorString(cudaStatus));
		return cudaStatus;
	}

#ifdef _DEBUG
	// Errors raised while the kernel runs only show up after a sync. Debug
	// only, since it stalls the CPU; waits for this stream only
	// (cudaDeviceSynchronize would also wait for TouchDesigner's own CUDA work).
	cudaStatus = cudaStreamSynchronize(stream);
	if (cudaStatus != cudaSuccess)
	{
		fprintf(stderr, "cudaStreamSynchronize returned error code %d after launching kernel!\n", cudaStatus);
	}
#endif

	return cudaStatus;
}
