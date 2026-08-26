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

#include "cuda_runtime.h"
#include "device_launch_parameters.h"

#include <algorithm>
#include <stdio.h>

#include "POP_CPlusPlusBase.h"

using namespace TD;

__device__ void
shiftZImpl(void* buffer, uint32_t numPoints)
{
	unsigned int x = blockIdx.x * blockDim.x + threadIdx.x;

	if (x >= numPoints)
		return;

	float3* pos = (float3*)buffer;
	pos[x].z += 1.0f;
}

__global__ void
shiftZ(void* buffer, uint32_t numPoints)
{
	shiftZImpl(buffer, numPoints);
}

__global__ void
shiftZ(void* buffer, POP_PointInfo* pointInfo)
{
	shiftZImpl(buffer, pointInfo->numPoints);
}

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

__global__ void
copyAndShiftZ(void* src, void* dst, uint32_t numPoints)
{
	copyAndShiftZImpl(src, dst, numPoints);
}

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

cudaError_t
shiftZPosition(const OP_SmartRef<POP_Buffer>& posBuffer, void* newDst, const OP_SmartRef<POP_Buffer>& pointInfo, const POP_MaxInfo& maxInfo, cudaStream_t stream)
{
	cudaError_t cudaStatus;

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

#ifdef _DEBUG
	// any errors encountered during the launch.
	cudaStatus = cudaDeviceSynchronize();
	if (cudaStatus != cudaSuccess)
	{
		fprintf(stderr, "cudaDeviceSynchronize returned error code %d after launching kernel!\n", cudaStatus);
	}
#else
	cudaStatus = cudaSuccess;
#endif

	return cudaStatus;
}
